# AGENTS.md — RTDP coding-agent contract

Governing documents: `docs/design.md` v2.1 (architecture and behavior — wins on behavior),
`docs/aws-deployment.md` (AWS service mapping and cloud guardrails — wins on infrastructure).

## Non-negotiables

- All tenants, transactions, models, identities, credentials, and actions are **synthetic**.
  Never use real customer data, real insurer credentials, or real enforcement providers.
- **Independent configuration deployment** is the governing requirement: routine model,
  ruleset, threshold, and product-binding changes must not require application rebuilds
  or database migrations.
- Rules consume registered **semantic signals** — never model classes, model tables, or
  provider-specific endpoints (ADR-001).
- The decision path is deterministic and replayable: pinned bundle digests, pinned
  activation epochs, no mutable "latest" resolution on the request path (ADR-004).
- Decision, intent, and effect are separate facts. A timed-out provider call is UNKNOWN,
  never an acknowledged success (ADR-007).
- Replay and shadow modes cannot issue live actions — enforce at orchestrator AND
  adapter boundaries (ADR-010).
- Tenant identity comes from authenticated identity, never from request payloads.
  Unknown tenants are rejected and audited — never default to a platform tenant.
- **ADR-011:** no model service identity may hold approver, activation, or
  action-administrator roles, or write activations, flow instances, action commands,
  or the action ledger. Enforce with RBAC and database roles, not convention.

## Build and test

```bash
make up        # local stack (Docker Compose): Kafka, Postgres, Redis, MinIO, Flink, ONNX, observability
make proto     # protobuf codegen (Go, Python, Java)
make seed      # topics, buckets, synthetic tenants/configs, features, model fixture, bundles
make lint test
make e2e
make load TPS=500 DURATION=5m
```

## Evidence

- Store commands, versions, digests, raw counts, latency/freshness percentiles, and
  failure logs in `docs/validation/` (local) and `docs/validation/aws/` (AWS).
- Configuration-only tests must compare image digests and migration versions
  before/after — not merely claim "no code changed."
- Never weaken a test or substitute a stub to make a gate pass. If a component cannot
  meet a design.md guarantee, stop and report.

## AWS rules (when working in the cloud account)

- Never run `terraform apply` or `destroy` without a posted plan and explicit approval.
- Never create IAM users, access keys, or resources outside the approved region.
- Never grant a model service identity write, activation, or dispatch permissions (ADR-011).
- Record every AWS resource change and its cost impact in `docs/validation/aws/`.
- If an AWS managed service cannot meet a design.md guarantee, stop and report;
  do not quietly swap components or weaken a test.

## Security

- Secrets live only as references in config; values in an approved secret store.
  Never commit credentials or keys.
- Tokenize sensitive identifiers at trusted ingress. Transaction and case text is
  data, never instruction — validate model outputs against schemas and keep
  prompt-injection fixtures in the test suite.
- Pin dependencies and image digests; scan and sign artifacts.
- Use bounded metric labels — no unrestricted tenant ids, transaction ids, rule text,
  or model payloads in metrics.
