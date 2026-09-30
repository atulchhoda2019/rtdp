# RTDP benefits demo — use cases BUC-1..BUC-5

A second product domain — benefits administration — running on the **same
platform as configuration**. No service code changed: everything below is
signal contracts, ONNX/SLM artifacts, provider bindings, rulesets, action
policies, products and tenant overlays under `contracts/`, `ml/` and
`assets/seed/`.

All tenants, participants and data are synthetic. "Client A"/"Client B"
map to `demo-client-a`/`demo-client-b` (→ `tenant_a`/`tenant_b`).

## Run it

```bash
make up && make seed                 # seeds all seven products
python tools/demo/benefits_demo.py            # narrated run-through
python tests/e2e/test_benefits_use_cases.py   # assertion suite
RTDP_INGRESS=<aws-endpoint> python tests/e2e/test_benefits_use_cases.py
```

Optional deeper probes for the suite: `RTDP_PSQL` (psql prefix),
`RTDP_INFERENCE_ADDR`, `RTDP_ORCH_ADDR` — see the test file header.

## BUC-1 — dependent_verification@1 (SLM fast-track)

| | |
|---|---|
| Event | `DEPENDENT_VERIFICATION` |
| Runtime profile | `document_intake` (30s product budget; SLM path) |
| Signal | `dependent.doc_consistency@1.0.0` — SLM `slm_dependent_verification@1` |
| Rules | `dependent_verification_policy@1` |
| Actions | `ENROLL_DEPENDENT` on APPROVE; `ROUTE_TO_REVIEW_QUEUE` on REVIEW — **no DECLINE** |

A digest-pinned prompt template (`ml/slm-dependent-verification/`) asks the
SLM to score consistency between the submitted document's declared fields
and the participant record. Rules consume the semantic signal:

- `consistency ≥ auto_accept (0.75)` → APPROVE → `ENROLL_DEPENDENT`
- `consistency < refer_below (0.5)` → REVIEW → review queue
- between → REVIEW (`DOCUMENT_BORDERLINE`); missing signal → REVIEW

**Proves:** the SLM is a bounded signal provider like any ONNX model —
contract-typed output, deadline, pinned artifact. The action-mapping table
is data: the product *cannot* decline — "AI approves, humans handle the
rest." Identical retries return the same pinned `decision_id` (replayable
audit). The test suite asserts no action rule binds `DECLINE`.

## BUC-2 — hsa_reimbursement@1 (receipt auto-adjudication)

| | |
|---|---|
| Event | `HSA_CLAIM` |
| Runtime profile | `decisioning` (100ms) |
| Signal | `hsa.eligibility_probability@1.0.0` — ONNX `hsa_eligibility_logistic@1` |
| Features | reuses tier-1 `claimant_claim_count_1h`, `claimant_amount_sum_24h` |
| Rules | `hsa_policy@1` |
| Actions | `ISSUE_REIMBURSEMENT` on APPROVE; `ROUTE_TO_REVIEW_QUEUE` on REVIEW |

Same product, same model, same $320 receipt:

- **Client A** (`tenant_a`, product default `auto_approve_limit=$500`) →
  `DECISION_APPROVE / AUTO_ADJUDICATED` → `ISSUE_REIMBURSEMENT`
- **Client B** (`tenant_b`, overlay `auto_approve_limit=$250`) →
  `DECISION_REVIEW / OVER_AUTO_APPROVE_LIMIT` → review queue

Velocity (`claimant_claim_count_1h > 5`) and low model eligibility
(`probability < 0.65`) also route to REVIEW.

**Proves:** allowlisted per-client overlays (`thresholds.*` only, enforced
by the compiler) diverge outcomes at scale; each tenant resolves its own
pinned `bundle_digest`.

## BUC-3 — live plan change, configuration only

```bash
python tools/demo/benefits_live_change.py
```

Compiles `tenant_b`'s HSA overlay `auto_approve_limit: 250 → 400` into a
new bundle and activates it under a new epoch (the activation manifest is
the only write the request path sees):

| | outcome | bundle_digest | manifest_epoch |
|---|---|---|---|
| $320 before | `REVIEW` | `sha256:…` | `e0` |
| $320 after  | `APPROVE` | `sha256:…` (different) | `e1` (new) |
| $320 rollback | `REVIEW` | restored digest | `e2` |

The script asserts and records in `docs/validation/phase3-config-only.json`:
service image digests **identical** before/after and the migration file
set **identical** — a config change, not a deploy.

## BUC-4 — contribution_change@1 (ambiguous external effect)

| | |
|---|---|
| Event | `CONTRIBUTION_CHANGE` |
| Runtime profile | `decisioning` (100ms) |
| Signals | none — rules over tier-1 features only |
| Rules | `contribution_policy@1` |
| Actions | `APPLY_CONTRIBUTION_CHANGE` → `local_timeout_simulator@1`; `OPEN_FRAUD_CASE` → `local_siu_case_adapter@1` |

- First-time participant, small change ($200) → APPROVE → apply intent
- First-time participant, large change ($8000) →
  `REVIEW / ACCOUNT_TAKEOVER_PATTERN` → fraud case
- Cumulative 24h volume over the plan cap → `REVIEW / PLAN_LIMIT_EXCEEDED`

The apply adapter deterministically times out *after* a possible provider
effect, so `action_execution.state` lands `UNKNOWN` with a preserved
`provider_reference` and `on_unknown_outcome: RECONCILE` — decision,
intent and effect are separate facts; there is no blind retry.

**Documented limits** (`docs/validation/phase3-gaps.md`): the
`recent_bank_change_days` predicate needs request `attributes` plumbed to
rules `Input` (runtime change — expressed here with the equivalent tier-1
history heuristic), and no automated reconciler exists yet — `UNKNOWN` is
the terminal row with the evidence a reconciler would consume.

## BUC-5 — shadow challenger (`hsa_eligibility_challenger@1`)

A second HSA model — same contract and feature schema, different training
seed — is trained, digest-pinned, bound (`hsa-eligibility-challenger@1`),
compiled (`hsa_reimbursement@2`) and activated for `tenant_a` under
`cohort: "shadow"` in the same manifest as the champion.

- **Champion/challenger differences:** `InferenceService.Score` on an
  identical feature vector returns different pinned probabilities.
- **Shadow safety (ADR-010):** `Orchestrator.Decide` with `MODE_SHADOW`
  yields a decision with **zero** `action_execution` rows — suppression at
  the orchestrator, drop at the adapter boundary.

**Documented limit** (`docs/validation/phase3-gaps.md`): live activation
lookup is first-match on tenant+env+event — cohort-aware shadow *routing*
isn't wired, so the champion continues to serve live traffic while the
shadow activation pins the challenger artifact for inspection.

## Where everything lives

```
contracts/signal_contracts/hsa.eligibility_probability/1.0.0.yaml
contracts/signal_contracts/dependent.doc_consistency/1.0.0.yaml
ml/seed-model/train.py                          hsa champion + challenger
ml/slm-dependent-verification/                  pinned prompt + schema
assets/seed/platform/bindings/                  3 new bindings
assets/seed/platform/rulesets/                  3 new rulesets
assets/seed/platform/action_policies/           3 new action policies
assets/seed/platform/products/                  4 new product versions
assets/seed/tenants/*/subscription.yaml         subscriptions + B overlay
tools/seed/seed.py                              seed wiring + shadow act.
tools/demo/benefits_demo.py                     narrated runner
tools/demo/benefits_live_change.py              BUC-3 live change
tests/e2e/test_benefits_use_cases.py            assertion suite
docs/validation/phase3-gaps.md                  honest runtime limits
docs/validation/phase3-config-only.json         BUC-3 evidence (generated)
```
