# Phase 3 gaps — benefits use cases vs the current runtime surface

The benefits demo scope (docs/demo-benefits.md) was implemented under the
rule: **no changes under `services/`, `internal/`, `cmd/`, `proto/` or the
ingress demo page.** Two use-case elements hit real limits of the current
request/activation envelope. They are documented here rather than hacked
around.

## G-1: Arbitrary request fields do not reach rules (BUC-4)

**Intended:** `recent_bank_change_days < 7 && amount > threshold` →
REVIEW + `OPEN_FRAUD_CASE`, and other plan-limit predicates over
submission-specific fields.

**What exists:** `internal/ruleseng/engine.go` exposes an `input` CEL
variable, and `AuthenticatedTransaction.attributes` exists in the proto —
but the orchestrator constructs the rules request with an empty `Input`
map and the ingress JSON schema has no arbitrary-attributes field. So
`input.*` and benefits-specific submission fields are unreachable from
CEL today without touching `services/` + `internal/` (forbidden).

**What was done instead:** `contribution_policy@1` expresses the same
guardrail *class* over the registered tier-1 features that do reach CEL:

- `account_takeover_pattern`: first-ever request from this participant
  (`claimant_claim_count_1h <= 1` — the counter includes the current
  event) with a large cumulative amount → REVIEW + `OPEN_FRAUD_CASE`.
- `plan_limit_exceeded`: cumulative 24h change volume over the plan cap
  → REVIEW.

This is a faithful demonstration of "rules over existing inputs as
configuration"; the `recent_bank_change_days` predicate itself requires
plumbing `attributes` → rules `Input`, which is a runtime change.

**To close:** populate `rulesreq.Input` from
`AuthenticatedTransaction.attributes` in the orchestrator and accept a
bounded `attributes` object in the ingress schema (both runtime changes).

## G-2: Shadow-cohort routing is not wired (BUC-5)

**Intended:** a `SHADOW` cohort activation for `tenant_a` serving the
challenger bundle in parallel with live traffic.

**What exists:** `Activation` carries a `cohort` field and seed writes a
`cohort: "shadow"` entry into `activations.json`, but
`internal/bundle/bundle.go` `ActivationFor` matches only
tenant+environment+event_type and returns the **first** match — cohort is
never consulted, and ingress always sends `MODE_LIVE`. There is no
traffic duplication to a shadow bundle on the request path.

**What was done instead:**

- The challenger is real end to end: `hsa_eligibility_challenger@1` is
  trained, digest-pinned, bound via `hsa-eligibility-challenger@1`, and
  deployed (artifacts + compiled `hsa_reimbursement@2` bundle + a
  `cohort: "shadow"` activation row for `tenant_a`).
- Champion-vs-challenger decision differences are shown by scoring the
  same feature vector through `InferenceService.Score` for both pinned
  models (`tests/e2e/test_benefits_use_cases.py`,
  `BUC-5 champion_challenger_differ`).
- The ADR-010 guarantee is exercised directly: `Orchestrator.Decide`
  with `MODE_SHADOW` produces a decision while the orchestrator
  suppresses `rtdp.action.commands` for any non-LIVE mode (and the
  dispatcher drops non-LIVE commands at the adapter boundary as
  defense-in-depth). The test asserts zero `action_execution` rows for
  the shadow decision id.

**To close:** teach `ActivationFor` (and the control-plane activation
write path) cohort semantics — e.g. `MODE_SHADOW` requests resolve the
shadow-cohort activation for the tenant while `MODE_LIVE` continues to
resolve the champion. That is a runtime change to `internal/` +
`services/`.

## G-3: UNKNOWN is terminal; no automated reconciler (BUC-4)

**Intended:** `UNKNOWN → RECONCILING → ACKNOWLEDGED (or manual)`.

**What exists:** `local_timeout_simulator` returns `acked=false, err=nil`,
so the dispatcher lands `action_execution.state = 'UNKNOWN'` and
publishes an `action.status` record — decision ≠ intent ≠ effect is
real, and no blind retry occurs. There is no reconciliation loop in the
codebase (`RECONCILING` exists only in the design state machine and in
`on_unknown_outcome` policy data).

**What was done instead:** the demo/test asserts the `UNKNOWN` terminal
state plus the preserved `provider_reference` — exactly the evidence a
reconciler (or a human) needs to resolve it. Adding the reconciler is a
runtime change; `on_unknown_outcome: RECONCILE` is already declared in
the policy so the seam is explicit.

## Not a gap

- BUC-1, BUC-2, BUC-3 are fully expressible in the existing contract
  surface: new event types, new signal contracts, a pinned SLM prompt
  template, new ONNX artifacts, allowlisted `thresholds.*` overlays, and
  action policies — all configuration.
