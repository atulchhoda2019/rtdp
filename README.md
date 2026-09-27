# RTDP — Real-Time Decisioning Platform (synthetic)

Multi-tenant, real-time decisioning platform built to `docs/design.md` v2.1.
The demo domain is **insurance claim decisioning**: tenants submit claim
transactions (`CLAIM_SUBMISSION` events) which are scored for fraud risk and
adjudicated in real time.

Everything in this repo is synthetic: synthetic tenants, claims, model,
identities, and simulated action providers only. No real policyholder
data, credentials, or enforcement providers.

## Synthetic domain

| Concept | Demo value |
|---------|-----------|
| Event | `CLAIM_SUBMISSION` (claim filed via `PORTAL`/`BROKER` channels) |
| Entities | `tokenized_claimant` (insured, tokenized at ingress), `provider_id` (repair shop, clinic, ...) |
| Product | `claim_decisioning@1` |
| Signal | `claim.fraud_probability@1.1.0` — `claim_fraud_logistic@2` ONNX model |
| Tier 1 features | `claimant_claim_count_1h`, `claimant_amount_sum_24h` (Redis Lua, atomic) |
| Tier 2 features | `provider_claim_count_1h`, `provider_amount_sum_1h` (Flink 1-min event-time tiles) |
| Outcomes | `APPROVE` / `REVIEW` / `DECLINE` |
| Actions | `CLAIM_RESPONSE` (inline, simulated), `OPEN_SIU_CASE` (durable simulated SIU adapter) |

Request path: `ingress → orchestrator → feature-service → signal-resolver
→ inference (ONNX) → rules-service (CEL) → Kafka transactional commit →
action-dispatcher (simulated effects)`.

## Stack

| Layer | Local (Docker Compose) | AWS (`infra/terraform`) |
|-------|------------------------|-------------------------|
| Streaming | apache/kafka:3.9.1 | MSK (3 brokers, 3 AZs, IAM TLS) |
| Durable facts | postgres:16.6 | Aurora PostgreSQL |
| Tier 1/2 store | redis:7.4.2 | ElastiCache Valkey 8 |
| Artifacts | adobe/s3mock:4.9.1 | S3 (+KMS, versioning) |
| Streaming compute | flink:1.20.0 | Managed Service for Apache Flink |
| Inference | onnxruntime (Python gRPC) | ONNX on EKS CPU nodes |
| Orchestration | Go services | EKS + ArgoCD |

## Quick start

```bash
make up      # bring up the full local stack
make proto   # regenerate protobuf code (Go/Python/Java)
make seed    # topics, buckets, model upload, bundles, Flink job, warmup
make e2e     # end-to-end decision + simulated action
make lint test
make load TPS=500 DURATION=5m
```

Requires: Go, Python 3.11+ (`make venv` provisions `.venv`), Docker
(colima on macOS), Java 21 + Maven (Flink jar), buf.

## Validation

`docs/validation/phase1.md` — seven acceptance gates (e2e, tenant
isolation, config-only change, semantic-incompatibility rejection,
stream recovery, model parity, action ambiguity) plus load results.
Raw evidence: `phase1-gates.json`, `load-*.json`.

```bash
.venv/bin/python tests/gates/phase1.py        # all gates
.venv/bin/python -m pytest tests/contracts   # contract tests
```

## AWS

`infra/terraform/bootstrap` is a verify-only module that checks account
guardrails (correct account, region, permission boundary, state bucket,
budget cap) and creates nothing. `infra/terraform/envs/sandbox` wires the
modules. `docs/aws-guardrail-setup.md` lists what you must provision
first. `terraform apply` is never run without a posted plan and explicit
approval — see `AGENTS.md`.

## Governing docs

- `docs/design.md` — architecture and behavior (authoritative)
- `docs/aws-deployment.md` — AWS service mapping and cloud guardrails
- `AGENTS.md` — non-negotiables (tenant binding, pinned bundles,
  intent/effect separation, ADR-011 model-identity restrictions, ...)
