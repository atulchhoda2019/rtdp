# RTDP demo use cases

Each use case lists the scenario, the expected behavior, and what it proves
about the platform. Every case is exercised by `tests/e2e/test_use_cases.py`
(`RTDP_INGRESS=<base> python tests/e2e/test_use_cases.py`), which also records
raw responses to `docs/validation/aws/` when run against the sandbox.

All data is synthetic: tenants (`demo-client-a` → `tenant_a`,
`demo-client-b` → `tenant_b`), claimants, providers, models, actions.

## UC-01 — Fast-path claim decision

`POST /v1/decide`, `CLAIM_SUBMISSION`, fresh claimant.

- Expected: 200, outcome in {APPROVE, REVIEW, DECLINE} from
  `claim_fraud_policy@5` over the `claim.fraud_probability` signal.
  Observed on sandbox: `DECISION_APPROVE`, ~0.2–0.4s warm.
- Proves: end-to-end path (ingress → orchestrator → tier-1 features →
  ONNX model → rules → durable Kafka commit → action intent) inside the
  product's `total_deadline_ms: 100` budget.

## UC-02 — Underwriting review band

`POLICY_APPLICATION`, either tenant.

- Expected: 200. Observed: `DECISION_REVIEW` + `UW_REFER_BAND` — the
  `uw_eligibility_logistic` score lands inside the seeded review band.
- Proves: model-signal → ruleset banding → non-approve outcomes route to
  review instead of binary approve/decline.

## UC-03 — Risk pricing quote

`QUOTE_REQUEST`, either tenant.

- Expected: 200. Observed: `DECISION_APPROVE` + `QUOTE_ISSUED` —
  `premium_linear` prices the quote and the action intent carries it.
- Proves: a second model family (pricing, not fraud) on the same pipeline.

## UC-04 — Document intake with SLM narrative check

`CLAIM_DOCUMENT_INTAKE`, either tenant.

- Expected: 200. Observed: `NARRATIVE_CONSISTENT` → APPROVE or
  `NARRATIVE_INCONSISTENT`/`MISSING_REQUIRED_SIGNAL` → REVIEW; ~2–12s on CPU
  (product budget 30s, not 100ms — this product has its own runtime profile).
- Proves: the SLM path (slm-service → Ollama qwen2.5:0.5b) as a *signal
  provider* — rules consume `narrative.consistency`, never the model
  endpoint itself (ADR-001). Also why ingress needs
  `RTDP_DECIDE_TIMEOUT_MS` > 5s.

## UC-05 — Tier-1 velocity enforcement

13× `CLAIM_SUBMISSION`, same fresh claimant, ~1s apart.

- Expected: requests 1–12 pass without `VELOCITY_LIMIT`; request 13
  returns `DECISION_DECLINE` + `VELOCITY_LIMIT`
  (`claimant_claim_count_1h > velocity_decline_count: 12`).
- Proves: tier-1 minute-bucket counters accumulate atomically in Valkey
  (Lua `HINCRBY` + `HEXPIRE` field expiry) across requests and pods.

## UC-06 — Idempotent retry

Same `transaction_id` + `transaction_revision` + identical payload, sent twice.

- Expected: identical `decision_id` on both responses — the dedup hash hit
  returns the cached vector without re-counting.
- Proves: replay safety — a retried request is not double-counted or
  double-committed.

## UC-07 — Payload conflict

Same `transaction_id`/`revision`, mutated payload (different amount).

- Expected: rejection (400/502 with `CONFLICT` in the error body).
- Proves: dedup is tamper-evident — the stored digest must match the
  payload, not just the transaction id.

## UC-08 — Unknown tenant rejection

Any request with `X-RTDP-Client-Id: rogue-client`.

- Expected: 401 `unauthenticated or unauthorized tenant`, before any
  decision work.
- Proves: tenant identity comes from the authenticated client fixture,
  never the payload (ADR/tenant boundary).

## UC-09 — Missing routing fields

`POST /v1/decide` omitting `tokenized_claimant` / `provider_id`.

- Expected: 400 `missing routing fields`.
- Proves: ingress validates the envelope before it reaches the pipeline.

## UC-10 — Per-tenant bundle pinning

Same event type (e.g. `CLAIM_SUBMISSION`) to `demo-client-a` and
`demo-client-b`.

- Expected: both 200, **different `bundle_digest` values** — each tenant
  resolves its own compiled bundle; `manifest_epoch` identical (same
  activation epoch).
- Proves: independent per-tenant configuration deployment; decisions are
  pinned to immutable digests — the replayability contract (ADR-004).

## UC-11 — Deterministic addressing in the response

Any successful response.

- Expected: `bundle_digest` (sha256 of the compiled bundle) and
  `manifest_epoch` (activation epoch) present.
- Proves: every decision carries the exact config+activation coordinates
  needed to replay it — no mutable "latest" on the request path.

## Coverage matrix

| Use case | test_use_cases.py check | Asserted |
|---|---|---|
| UC-01 | `fast_path_claim` | 200 + valid outcome |
| UC-02 | `underwriting_review` | 200 + valid outcome |
| UC-03 | `quote_pricing` | 200 + valid outcome |
| UC-04 | `document_intake_slm` | 200 + valid outcome (≤35s) |
| UC-05 | `velocity_limit` | DECLINE+VELOCITY_LIMIT at #13 |
| UC-06 | `idempotent_retry` | same decision_id |
| UC-07 | `payload_conflict` | non-2xx + CONFLICT |
| UC-08 | `unknown_tenant` | 401 |
| UC-09 | `missing_fields` | 400 |
| UC-10 | `tenant_bundle_pinning` | digests differ per tenant |
| UC-11 | `pinned_addressing` | digest + epoch present |
