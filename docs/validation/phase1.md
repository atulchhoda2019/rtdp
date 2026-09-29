# Phase 1 validation evidence — local Compose stack

All evidence is synthetic (synthetic tenants, transactions, model, and
simulated action providers only). Raw machine-readable results live beside
this file; commands below reproduce them.

## Environment

- macOS arm64, Docker via colima (`docker compose` in `docker-compose.yml`)
- Kafka `apache/kafka:3.9.1`, Postgres `postgres:16.6`, Redis `redis:7.4.2`,
  S3-compatible `adobe/s3mock:4.9.1` (local substitute for MinIO — see compose
  comments), Flink `1.20.0-java17`, ONNX Runtime `1.30.0` (Python 3.11),
  Ollama `qwen2.5:0.5b` (local SLM runtime)
- Models: `claim_fraud_logistic@2`
  (`sha256:32d410b0a029739e1f86b9ac4888bdfb40162f40bb12f3a2e401842b433f0b33`),
  `uw_eligibility_logistic@1`
  (`sha256:9e64a10a50a62d700f5fb1a1fd81b198ccb5a951d93c28110ee2cc2e6ff547ec`),
  `premium_linear@1`
  (`sha256:b5656cd7d04812e6854912ca176f0990e10979c6b536963ca26290cae794530f`),
  `slm_narrative@1` — Ollama blob `sha256:c5396e06af294bd1` + digest-pinned
  prompt template
- Products: `claim_decisioning@1` (CLAIM_SUBMISSION),
  `underwriting_decisioning@1` (POLICY_APPLICATION),
  `risk_pricing@1` (QUOTE_REQUEST),
  `document_intake@1` (CLAIM_DOCUMENT_INTAKE) — 2 tenants × 4 products =
  8 pinned bundles per activation manifest

## Contract tests (Phase 0)

```
.venv/bin/python -m pytest tests/contracts -q
# 13 passed — valid assets compile; malformed/incompatible assets rejected;
# all four products compile with per-binding input_features vectors and
# contract-carried value_schema
```

## Acceptance gates — `tests/gates/phase1.py`

Evidence: `phase1-gates.json`, `phase1-gates.log`.

| Gate | Result | Evidence |
|------|--------|----------|
| G1 e2e decision path | pass | 401 for unknown client; tenant_a `DECISION_APPROVE`, tenant_b `DECISION_APPROVE`; distinct pinned bundle digests; idempotent retry; payload conflict rejected; **multi-product routing**: CLAIM_SUBMISSION→claim_decisioning, POLICY_APPLICATION→underwriting_decisioning, QUOTE_REQUEST→risk_pricing, CLAIM_DOCUMENT_INTAKE→document_intake (verified by bundle digest); unrouted event type rejected |
| G2 tenant isolation | pass | `tenant_id` in request body ignored (tenant bound from `X-RTDP-Client-Id`); `decision_fact` row carries `tenant_a`; Redis dedup key scoped `rtdp:dedup:{tenant_a:LIVE}:…` |
| G3 config-only change | pass | tenant_b `thresholds.decline_probability` overlay 0.85 → 0.0; same transaction shape flips `APPROVE` → `DECLINE`; image digests and `pg_dump -s` schema hash identical before/after; no restart (activations.json re-read per request) |
| G4 semantic incompatibility | pass | binding emitting `claim.fraud_probability@9.9.9` and `no.such.contract@1.0.0` both rejected at compile: `SEMANTIC_INCOMPATIBLE` |
| G5 stream recovery | pass | TaskManager hard-restarted mid-window; job `ec354cb25a0c` resumed from checkpoint (gate now requires a post-restart completed checkpoint before closing the window); tier-2 tile `provider_claim_count_1h@1` count == 3 exactly (no feature inflation on replay) |
| G6 model parity | pass | ONNX Runtime vs sklearn on 256 probes per model — `claim_fraud_logistic` max \|Δp\| = 1.665e-16, `uw_eligibility_logistic` max \|Δp\| = 1.665e-16, `premium_linear` max \|Δ\| = 0 (regression) |
| G7 action ambiguity | pass | `local_timeout_simulator@1` adapter (times out after possible apply) → `action_execution.state = 'UNKNOWN'` with provider_reference; never `ACKNOWLEDGED`; no blind retry |

## Load test

`tests/performance/load.py` (raw reports: `docs/validation/load-<ts>.json`)

| Offered | Duration | Completed | Duplicates | Errors | p50 | p95 | p99 |
|---------|----------|-----------|------------|--------|-----|-----|-----|
| 200 TPS | 60 s | 11,952 | 0 | 0 | 32 ms | 51 ms | 62 ms |
| 400 TPS | 90 s | 31,322 | 0 | 1,710 (5.2%) | 15 ms | 191 ms | 489 ms |
| 500 TPS | 300 s | 120,182 | 0 | 11,789 (8.9%) | 30 ms | 182 ms | 297 ms |

All errors are `DeadlineExceeded` — the hard `total_deadline_ms=100`
contract bound rejecting under queue pressure, surfaced as designed (never
silent success, never partial commits; 0 duplicate decisions). Local
sustained clean rate ≈200 TPS on the 6-CPU colima VM; the 500 TPS SLO gate
is capacity-bound and is re-measured on EKS (Stage E).

Notable fix found by this gate: concurrent requests tripped a shared
franz-go transactional producer (`already in a transaction`); the commit
boundary now uses a `TxnPool` of per-flight producers
(`internal/kafkax/kafkax.go`).

## Known local-only deviations

- `adobe/s3mock` substitutes for MinIO (free MinIO images no longer pullable);
  S3 API surface only — documented in `docker-compose.yml`.
- Inference warm/Score uses real ONNX Runtime; Flink uses real event-time
  windows, watermarks, checkpointing, and a transactional Kafka sink.
- The 100 ms `execution.total_deadline_ms` is a hard contract bound; local
  Docker/Colima tails occasionally reject decisions on deadline — callers
  retry, which is exercised as designed behavior, not hidden.
- Host-facing Kafka uses the EXTERNAL listener on `localhost:29092`;
  containers use `kafka:9092`.

## AWS

- `terraform fmt -recursive` clean; `terraform validate` passes in
  `infra/terraform/bootstrap` and `infra/terraform/envs/sandbox`.
- `bootstrap` is a verify-only module (guardrail checks, no resources).
- No `terraform plan`/`apply` has been run; requires a posted plan and
  explicit approval per `docs/aws-deployment.md`.
- `docs/validation/aws/` will hold plan/cost evidence when AWS stages begin.
