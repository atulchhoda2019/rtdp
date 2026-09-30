# RTDP — Detailed Design (as built)

Companion to `docs/design.md` v2.1 (normative spec). This document describes
the implemented system: components, wire contracts, data flow, and failure
semantics as they exist in this repository today.

Everything is synthetic: tenants, claimants, providers, models, and action
endpoints are fixtures. No real policyholder data, no real binding
authority, no real payment or enforcement rails.

## 1. Products

A tenant subscribes to products; each request resolves to exactly one
pinned product bundle via `event_type` routing.

| Event type | Product | Signal contract | Model | Action types |
|---|---|---|---|---|
| `CLAIM_SUBMISSION` | `claim_decisioning@1` | `claim.fraud_probability@1.1.0` | `claim_fraud_logistic@2` | `CLAIM_RESPONSE`, `OPEN_SIU_CASE` |
| `POLICY_APPLICATION` | `underwriting_decisioning@1` | `underwriting.eligibility_probability@1.0.0` | `uw_eligibility_logistic@1` | `POLICY_RESPONSE`, `ASSIGN_UNDERWRITER` |
| `QUOTE_REQUEST` | `risk_pricing@1` | `pricing.premium_estimate@1.0.0` | `premium_linear@1` | `QUOTE_RESPONSE`, `REFER_ACTUARY` |
| `CLAIM_DOCUMENT_INTAKE` | `document_intake@1` | `claim.narrative_consistency@1.0.0` | `slm_narrative@1` (Ollama `qwen2.5:0.5b`) | `INTAKE_RESPONSE`, `OPEN_SIU_CASE` |

Signals are typed by contract, not by shape: `claim.fraud_probability` and
`underwriting.eligibility_probability` carry a bounded `probability` value;
`pricing.premium_estimate` carries a `premium` currency-amount scalar
produced by a regression model; `claim.narrative_consistency` carries a
`consistency` score produced by a pinned small language model. The
compiled bundle carries each contract's `value_schema`; the resolver
validates required fields, value kinds, and numeric ranges against it —
no field name is hardcoded in the validator. Rulesets interpret signals
per-product — underwriting uses eligibility bands (bind / refer /
decline), pricing applies rating bounds (`min_auto_quote` /
`max_auto_quote`), intake applies consistency bands (`refer_below` /
`auto_accept`) — with out-of-band results routing to `REVIEW`.

The SLM service implements the same `InferenceService` gRPC surface as
the ONNX service (`Warm`/`Score` keyed by digest), so the resolver routes
to it via a different `endpoint_ref` with no code change. The artifact is
an `ollama://` tag pinned by blob digest plus a digest-pinned
`prompt_template.json`; warm replays a golden input and requires a
parseable, in-range completion. Generative output is validated before it
becomes signal evidence (INVALID_INPUT, never fabricated). The intake
product's deadlines are sized for token generation
(`total_deadline_ms: 15000`), separate from the 100ms synchronous
products — the SLM path never sits inside a latency-critical envelope.

## 2. Request path

```
client ── HTTP /v1/decide ──► ingress ──► orchestrator ──► feature-service (Tier1 Lua + Tier2 read)
                              │                │
                              │                ├─► signal-resolver ──► inference-service (ONNX)
                              │                │                  └─► slm-service (Ollama)
                              │                │
                              │                ├─► rules-service (CEL)
                              │                │
                              │                └─► Kafka txn: decision_fact + contribution
                              │                     + action_commands + egress
                              ▼
                    action-dispatcher ──► simulated adapters ──► execution_event ledger
                    feature-materializer ◄── Flink (event-time tiles) ◄── feature.contrib topic
```

1. **Ingress** (`services/ingress`): HTTP→gRPC. Authenticates the caller by
   `X-RTDP-Client-Id` → tenant binding (synthetic demo auth). Rejects
   unknown clients with 401; tenant identity is never read from the body —
   a `tenant_id` field in the payload is ignored (G2 gate evidence).
2. **Orchestrator** (`services/orchestrator`): resolves the activation,
   dedupes the transaction, drives features → signals → rules, and commits
   all facts in **one Kafka transaction**.
3. **Feature service** (`services/feature-service`): Tier 1 atomic
   read-modify-write via `internal/store/tier1.lua`; Tier 2 reads of
   materialized `rtdp:t2:` tiles; returns the feature vector + snapshot
   digest + missing list.
4. **Signal resolver** (`services/signal-resolver`): fans out per-signal to
   the bound provider with the binding's deadline; builds each model's
   input vector from its declared `input_features`; converts rejections to
   typed signal status (`UNAVAILABLE`, `TIMED_OUT`, `INVALID_INPUT`,
   `MISSING_INPUT`).
5. **Inference service** (`services/inference-service`): ONNX Runtime;
   serves only warmed, digest-pinned artifacts; wraps the scalar output in
   the canonical `SignalEnvelope` named by the model's `output_contract`.
   **SLM service** (`services/slm-service`) implements the same
   `InferenceService` gRPC surface over Ollama: the pinned artifact is an
   `ollama://` tag (weights verified by blob digest) plus a digest-pinned
   prompt template; unparseable or out-of-range completions return
   `INVALID_INPUT` — never a fabricated value.
6. **Rules service** (`services/rules-service`): verifies the ruleset spec
   against its digest, compiles CEL once per digest, evaluates, and
   aggregates with `DECLINE > REVIEW > APPROVE` precedence.
7. **Action dispatcher** (`services/action-dispatcher`): consumes action
   commands, executes through the named simulated adapter, writes durable
   ledger rows (`execution_event`).

## 3. Configuration model

### 3.1 Compile-time assets (`assets/`, `contracts/`)

- **Signal contracts** (`contracts/signal_contracts/`): name, semver,
  value schema (typed values + ranges), semantics (meaning, population,
  target label, observation horizon, unit), allowed statuses,
  compatibility rules.
- **Feature definitions** (`contracts/feature_definitions/`): name,
  version, tier (`tier1` | `tier2`), window, entity key.
- **Products** (`assets/seed/platform/products/`): routing predicates
  (`event_types`, `channels`), execution budget (`total_deadline_ms ≤
  100`), required features, signal bindings, ruleset ref, thresholds,
  action policy ref, rollout.
- **Provider bindings** (`assets/seed/platform/bindings/`): provider,
  endpoint ref, model ref + digests (`model_digest`,
  `input_schema_digest`, `preprocessing_digest`), `output_contract`,
  **`input_features`** — the model's ordered input vector —
  `maximum_age_ms`, quality gates.
- **Rulesets** (`assets/seed/platform/rulesets/`): `requires` (named signal
  deps), CEL rules with `when`/`outcome`/`requires_signals`, default and
  missing-signal outcomes.
- **Action policies** (`assets/seed/platform/action_policies/`):
  `allowed_actions`, `adapters` (action → adapter ref), `action_rules`
  (outcome → permitted actions), `intent_ttl_ms`, retry policy,
  `on_unknown_outcome`.
- **Tenant subscriptions + overlays** (`assets/seed/tenants/`): product
  list with pinned versions and revisions; overlays scoped per product,
  allowlisted to `thresholds.*`, `routing.*`, `rollout.*`.

### 3.2 Compilation and activation

`rtdp_contracts.compiler.compile_bundle` resolves product + overlay into an
immutable **runtime bundle**: materialized effective config, feature refs
with digests, signal specs (contract digest, binding digest, model digests,
`input_features`), ruleset spec + digest, action policy spec. The bundle is
content-addressed (`canonical_digest`) and written to
`build/bundles/sha256_<digest>.json`.

`activations.json` pins `(tenant, environment, cohort) + event_type →
bundle` at an epoch. The orchestrator reads it **per request** — a config
change is a file swap, not a restart (G3 evidence: identical image
digests + schema hash across a threshold flip that changed APPROVE→DECLINE).

### 3.3 Routing

`internal/bundle.Store.ActivationFor(tenant, env, eventType)` selects the
activation whose bundle's `effective_config.routing.event_types` contains
the event. A bundle with unconstrained `event_types` is the fallback;
no match → the request fails closed. No mutable "latest" lookup exists
anywhere on the request path.

## 4. Feature planes

| | Tier 1 (synchronous) | Tier 2 (streaming) |
|---|---|---|
| Path | `internal/store/tier1.lua` in Redis | Flink job → `rtdp.feature.updates.v1` → materializer → `rtdp:t2:` tiles |
| Time | processing time | event time + watermarks + lateness |
| Guarantees | current event counted atomically; idempotent dedup | exactly-once committed tiles; checkpoint recovery |
| Features | `claimant_claim_count_1h`, `claimant_amount_sum_24h` | `provider_claim_count_1h`, `provider_amount_sum_1h` |

The Tier 1 Lua op (one atomic call): dedup check (`rtdp:dedup:{tenant:mode}:txn`
→ `OK`/`CONFLICT`/`REPLAYED`), horizon check, `HINCRBY`/`HINCRBYFLOAT` into
per-minute hash buckets, `HEXPIRE` per-field TTL, return the assembled
vector. A failed Tier 1 write yields an *absent* feature (fails closed), not
a stale one.

Flink consumes `rtdp.feature.contrib.v1` (one canonical contribution per
committed decision), computes 1-minute event-time tiles, and sinks absolute
values transactionally; the materializer writes `read_committed` updates to
Redis, so recovery cannot inflate counts (G5: TM restart mid-window → tile
count == 3 exactly).

## 5. Inference boundary

- `Warm` loads a pinned artifact by URI, verifies `model_digest` and
  `input_schema_digest`, validates golden vectors, and registers the model.
  Nothing scores until warmed — there is no implicit latest model.
- `Score` takes the binding's ordered `feature_names`/`feature_values` and
  returns a `SignalEnvelope` whose name/version come from the model's
  declared `output_contract`, `contract_digest` echoed from the request.
- `output_kind` selects output handling: `binary_probability` reads
  P(class=1) from the classifier probability tensor; `regression` reads
  the scalar estimate — this is how `premium_linear` emits a dollar figure
  behind the same envelope boundary.
- Parity evidence (G6): ONNX vs sklearn max |Δ| = 1.665e-16 (classifiers),
  0.0 (regression) over 256 probes each.

## 6. Rules and decision authority

CEL rules evaluate against `features`, `signals.<alias>`, `cfg`
(thresholds), `input`, `actor`, `timer` — compiled once per ruleset digest
with expression-size and cost limits. Rules declaring `requires_signals`
are *skipped* (not zero-valued) when those signals are absent; the
product's `missing_required_signal` outcome applies. Aggregation is
explicit precedence, never iteration order.

Outcome → action mapping is data, not code: the bundle's action policy
`action_rules` lists which decisions may emit which action type
(e.g. `ASSIGN_UNDERWRITER` only on `REVIEW`); `adapters` names the
simulated adapter per action. Intents get `tenant:env:decision:generation:
action:provider` idempotency keys and per-action TTLs.

## 7. Action lifecycle

Decision ≠ intent ≠ effect. Action commands are published inside the same
Kafka transaction as the decision fact; dispatch is async:

```
PENDING → DISPATCHING → ACKNOWLEDGED | FAILED | UNKNOWN → RECONCILING
```

A provider timeout after possible apply is **UNKNOWN** — reconciled, never
blind-retried (G7). Shadow/replay modes cannot emit live action commands
(orchestrator) and adapters reject non-LIVE modes (ADR-010). Adapters are
simulated: `*_simulator` returns a deterministic ref keyed on the
idempotency key; `*_queue_adapter`/`local_siu_case_adapter` write a durable
case row so retries see the existing case; `local_timeout_simulator`
exercises the UNKNOWN path.

## 8. Durability and isolation

- **Kafka** is the commit boundary: `decision.facts`, `feature.contrib`,
  `action.commands`, `egress` commit atomically (per-flight transactional
  producers via `internal/kafkax.TxnPool`). Consumers of updates run
  `read_committed`.
- **Postgres** holds projected facts (`decision_fact`, `action_intent`,
  `action_execution`, `execution_event`) via the projector/dispatcher.
- **Tenant isolation** is end-to-end: tenant bound from client identity,
  Redis keys hash-tagged `{tenant:mode}`, bundle digests differ across
  tenants, facts carry the authenticated tenant (G2).
- **ADR-011**: model/inference identities hold no activation, approval, or
  dispatch authority.

## 9. Local stack ↔ AWS

| Local | AWS (deployed) |
|---|---|
| kafka 3.9.1 | MSK `rtdp-sandbox`, 11 topics rf=3 / min.insync.replicas=2, IAM auth |
| postgres 16.6 | Aurora PostgreSQL `rtdp-sandbox` |
| redis 7.4.2 | ElastiCache **Valkey 9.0** (`HEXPIRE` needed server-side 9.0 here) |
| s3mock | S3 ×4 + KMS + versioning |
| flink 1.20 | Managed Flink `rtdp-features-sandbox` |
| onnxruntime gRPC | EKS CPU nodes |
| go services | EKS `rtdp-sandbox` + ArgoCD app-of-apps |
| local browser | Cloudflare quick tunnel → `edge-tunnel` pod (no ELB — boundary denies `CreateServiceLinkedRole`) |

Deployed and validated end-to-end — see `docs/aws-architecture.md` for the
as-deployed topology and `docs/validation/aws/` for evidence. Terraform
modules: `infra/terraform/modules/*`, envs under
`infra/terraform/envs/sandbox`, guardrail bootstrap under
`infra/terraform/bootstrap`.

## 10. Evidence

`docs/validation/phase1.md` + `phase1-gates.json`: seven gates (e2e,
tenant isolation, config-only change, semantic incompatibility, stream
recovery, 3-model parity, action ambiguity) — all passing on the
multi-product stack. Load: ~200 TPS clean locally, 500 TPS offered with
deadline-bound rejections and 0 duplicate decisions.
