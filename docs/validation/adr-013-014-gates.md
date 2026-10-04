# ADR-013 / ADR-014 gate evidence — 2026-10-04

Local Docker Compose stack (Colima), fully seeded (`make seed` complete:
16 tenant bundles, 5 ONNX models, 3 pinned SLM artifacts, 6 agents +
grants). All commands run from repo root with `.venv/bin/python`.

## G8 — delegation chain (ADR-013)

`python tests/e2e/test_agent_g8.py` → **10/10**

| Check | Result |
|---|---|
| Valid chain decides + delegation echoed | PASS `DECISION_APPROVE` |
| Legacy caller unchanged | PASS `DECISION_APPROVE` |
| Invalid signature | PASS 401 bad signature |
| Expired chain (1 s TTL mint) | PASS 401 link 0 outside validity window |
| Revoked grant denied ≤5 s | PASS 401, propagation **0.01 s** |
| Depth 3 > tenant max 2 | PASS 401 |
| Depth-2 chain accepted | PASS `DECISION_APPROVE` |
| Missing `benefits` scope | PASS `DECISION_DECLINE_UNAUTHORIZED` |
| Scoped chain evaluates | PASS `DECISION_PENDING_APPROVAL`¹ |
| Legacy on scoped product | PASS `DECISION_APPROVE` |

¹ `PENDING_APPROVAL` is the new normal outcome for a T1-capped agent
(`benefits-agent-1`) on a product whose actions include T2 — scope
still passed; ADR-014 holds the action instead of completing it.

## G9 — autonomy tiers + approval (ADR-014)

`python tests/e2e/test_agent_g9.py` → **13/13**

- G9a per-action autonomy: T2 agent + small amount → `APPROVE`, both
  intents `READY`. T2 agent + large amount → `PENDING_APPROVAL` with
  `APPLY_CONTRIBUTION_CHANGE` held but `NOTIFY_PARTICIPANT` still
  `READY` — per-intent autonomy, not atomic. T1 agent ceiling caps the
  T2 action → both held.
- G9b: held intents projected to `approval_held` (rows=2). Requesting
  agent 403, requesting principal 403 (self-approval barred), non-listed
  approver 403.
- G9c: `role:plan_admin` releases → dispatcher reaches `ACKNOWLEDGED`
  in `action_execution`. `REJECT` → `CANCELLED`, never dispatched.
- G9d: REVIEW decision emits T0 `PLAN_AUDIT` (sla_seconds: 6,
  `on_sla_breach: ESCALATE`); sweeper escalated it → `state=ESCALATED`,
  routed to `role:ops_director`, due_at extended 24 h.
- G9e: ownerless action policy fails compilation:
  `ContractError MISSING_OWNER`.

## Regression

- `test_e2e.py` — ok (6/6 scenarios)
- `test_use_cases.py` — 11/11
- `test_benefits_use_cases.py` — 13/13 (2 env-gated skips)
- `make lint test` — go test all packages + 17 pytest contracts green

## Notes for reviewers

- `maximum_age_ms`/`timeout_ms` are real enforcement: cold model loads
  legitimately produce `MISSING_REQUIRED_SIGNAL` REVIEWs. Tests warm
  the path first and treat a signal-timeout REVIEW as a deadline
  rejection (same class as `DeadlineExceeded`), retrying the
  transaction — not a weakening: the asserted reasons/outcomes are
  unchanged.
- Held commands carry approvers/SLA/requester identities on the
  durable `ActionCommand` — approval-service resolves no bundles.
