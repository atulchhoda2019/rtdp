# RTDP: Configurable Fraud and Risk Decisioning
Detailed system design • Version 2.1 • September 27, 2026 • Implementation target: Devin
Version 2.1 adds declarative state flows and bounded language-model roles (ADR-011, ADR-012). All other v2.0 content is unchanged.
RTDP is a configurable, multi-tenant platform for turning transaction events and model-generated risk signals into explainable decisions and authorized real-time actions. Its governing requirement is independent evolution: routine model, ruleset, threshold, and product-binding changes must deploy through validated configuration without changing application code or datastore schemas.
This document supersedes design.md v1.1. It retains real Flink streaming and separate AI inference in the MVP, introduces a stable signal boundary, and specifies tenant-aware catalog, product selection, governance, and action execution. rtdp-architecture.excalidraw contains the corresponding views; root AGENTS.md is the coding-agent contract.
All examples use synthetic tenants, transactions, models, and thresholds. This is a proposed reference implementation, not a description of an existing production deployment, an assertion of compliance, or a measured performance result. Do not upload the original source PDF or business-specific material to a third party without authorization.
## Reading guide

| Section | Implementation question |
|---|---|
| Requirements and decision status | What is confirmed, proposed, or unresolved? |
| Core tenets | What must remain true as the platform evolves? |
| Logical architecture | Which component owns each responsibility? |
| Tenant-aware catalog and product selection | Which product/configuration may this request use? |
| Signal contract and compatibility | How do models and rules evolve independently? |
| Configurable execution and worked example | How is a transaction evaluated? |
| Flink and feature consistency | What does streaming compute, and what does it guarantee? |
| Real-time action execution | Who may act, and how is completion established? |
| Declarative state flows and model-assisted authoring | How do multi-step states evolve, and where may language models participate? |
| Persistence, APIs, and topics | What must Devin implement? |
| Lifecycle, reliability, and security | How do changes and failures remain controlled? |
| Delivery plan and acceptance tests | How do we prove the design works? |

## Requirements and decision status
### Confirmed requirements
**Configurable decisions:** Rules, model bindings, thresholds, product flows, and policies must not be hard-coded for each product.
**Independent evolution:** Models and rules must be loosely coupled. Routine changes must not wait for application-code or datastore-schema changes.
**Real streaming and scoring:** Flink and a dedicated AI inference capability are required in the first functional MVP.
**Product context:** Product catalog and product selection are required design topics.
**Multi-tenancy:** Catalog ownership, visibility, subscriptions, configuration, and runtime isolation must account for tenants.
**Real-time outcomes:** The platform must support fraud/risk decisions and authorized actions, not merely calculate scores.
**Buildable handoff:** Deliver a detailed Markdown specification, editable Excalidraw architecture, and implementation guidance for Devin.
### Proposed defaults, not established business facts

| Decision | Proposed reference implementation | Confirmation needed before production |
|---|---|---|
| Tenant topology | Flat tenants; optional parent pointer reserved, no automatic inherited access | Actual legal/business hierarchy and delegated administration |
| Product ownership | Platform-owned shared definitions plus tenant-owned products | Which catalog assets may be shared or sold |
| Subscription | Explicit tenant subscription to a pinned product version | Enrollment, commercial entitlement, billing |
| Override scope | Allowlisted thresholds, bindings, action permissions, and routing | Which controls tenants may weaken or strengthen |
| Model transport | Synchronous gRPC in Phase 1; common signal interface permits pre-scored events and stream transport later | Whether external producers must be in the first live release |
| Aggregation | Explicit precedence policy, not implicit highest-product-score wins | Business-approved conflict resolution |
| Actions | Local simulated response, case, and notification adapters | Actual enforcement authority, providers, deadlines |
| Missing required score | REVIEW fallback; no fabricated zero probability | Per-product fail-open/fail-closed policy |
| Local stack | Go, Python, CEL, ONNX Runtime, Flink, Kafka-compatible broker, Postgres, Redis, MinIO, React | Production platforms, approved runtimes, licensing |

“Support an interface” does not mean “implement every transport now.” This design proposes a provider interface with synchronous inference as the initial real implementation; external score ingestion is a separately testable extension.
### Explicit non-goals
**No arbitrary self-modification:** Configuration cannot execute uploaded Python, JavaScript, SQL, shell commands, or unapproved network requests.
**No universal zero-code promise:** A new model runtime, unsupported feature operator, external data connector, or action integration may require code.
**No production authority in the demo:** All identities, claims, credentials, endpoints, and actions are synthetic or local simulations.
**No guaranteed business accuracy:** Synthetic model performance and thresholds demonstrate mechanics, not fraud efficacy.
**No global exactly-once assertion:** Guarantees are defined separately for Flink state, Kafka records, serving stores, and external side effects.
## Core tenets

| Tenet | Design rule | Acceptance evidence |
|---|---|---|
| Configuration is executable data | Product behavior is a validated declarative bundle, not product-specific branches | Change a threshold without rebuilding services |
| Models publish signals; rules express policy | Rules consume named semantic signals, never model classes or model tables | Swap compatible model artifacts while leaving rule source unchanged |
| Stable envelope, extensible validated payload | Store identity/version/time in stable fields and registered values in typed payloads | Add an optional signal without an application rebuild or SQL migration |
| Semantic compatibility matters | Units, meaning, target label, horizon, calibration, and population are contract fields | Reject percent-as-probability and changed-label examples |
| Deployment is not activation | A loaded model becomes eligible only through an approved binding | Deploy candidate but leave champion behavior unchanged |
| Tenant identity is a security boundary | Derive tenant from authenticated identity and enforce it throughout | Same event/entity ids in two tenants never collide |
| Every decision pins a configuration | One manifest and one resolved input snapshot govern an execution | Concurrent activation cannot create mixed-version decisions |
| Loose coupling is not zero waiting | Required signals have bounded waits and explicit fallback branches | Missing signal reaches a terminal outcome by deadline |
| Decision and effect are different facts | Record action intent, dispatch, acknowledgment, and unknown outcome independently | A timed-out provider call never appears as acknowledged success |
| Streaming and serving are distinct | Flink computes asynchronous features; Tier 1 preserves synchronous update semantics | Current-event test succeeds for Tier 1 without assuming Tier 2 visibility |
| Replay is safe by default | Shadow/replay have isolated state and cannot issue live actions | Replaying production-shaped input causes zero real side effects |
| Evidence accompanies change | Contract, replay, isolation, and operational tests gate activation | Release contains test results and artifact digests |

### What “configuration-only” means
For a supported runtime and contract, updating a model artifact, ruleset, threshold, subscription binding, feature definition expressed in supported operators, or action policy does not require modifying service source or running a database migration. It may still require validation, model loading, a Flink savepoint/job update, index provisioning, approval, and staged activation.
Configuration can change policy, not invent capabilities. New operators, integrations, incompatible protocol envelopes, new query/index requirements, or stronger isolation tiers are engineering changes and must be labeled accordingly.
### Three kinds of coupling
**Release coupling:** Avoid requiring model, rule, UI, and schema releases to move together for routine changes.
**Data coupling:** Share only registered contracts and immutable references, not another service’s private datastore.
**Execution dependency:** Preserve necessary sequencing. A rule requiring a model signal must wait within budget or take its declared fallback.
## Logical architecture
### Planes
```
CONTROL PLANE
  Tenant identity/entitlements
  Product catalog + subscriptions + configuration overlays
  Signal/feature contracts + model/rule/flow/action registries
  Validation + simulation + approval
  Bundle compiler + immutable artifacts + activation manifest
                                    |
                          prepare, verify, activate
                                    v
DECISION EXECUTION
  Authenticated ingress → tenant resolution → product selection
    → pin effective bundle → dependency plan
    → feature service → signal resolver → declarative rules
    → aggregation → decision + permitted action intents
                                    |
                         durable Kafka transaction
                                    v
  Egress + canonical contributions + audit facts + action commands
                                    |
             +----------------------+---------------------+
             v                      v                     v
STREAMING FEATURES            ACTION EXECUTION       ANALYTICS
  Flink event-time jobs        tenant adapter policy  immutable journal
  transactional updates       idempotency ledger     trace/search views
  materializer → online store provider/simulator     replay/evaluation
             |                ACK/UNKNOWN/FAILURE     governed feedback
             +→ future feature reads
```
### Service responsibilities

| Component | Owns | Must not own |
|---|---|---|
| Ingress adapter | Authentication, tenant binding, tokenization boundary, schema limits, deadlines | Tenant access inferred from untrusted payload |
| Catalog/control API | Definitions, subscriptions, lifecycle, permissions, overrides | Per-transaction database lookup on the hot path |
| Bundle compiler | Dependency resolution, type checking, effective tenant config, digest | Transaction execution |
| Product Selection module/service | Eligible subscribed products and exact config refs | Model scoring or hidden business fallbacks |
| Orchestrator | Pinned context, DAG scheduling, deadlines, result aggregation | Feature storage implementation or model-specific code |
| Feature Service | Atomic Tier 1 update, versioned Tier 2 read, metadata | Rule decisions or duplicate contribution publication |
| Signal Resolver | Provider abstraction, envelope validation, correlation, freshness, admissibility | Arbitrary reinterpretation of incompatible scores |
| Inference Service | Load/warm approved artifact, preprocess, score, return signal envelope | Tenant action authority or direct external side effects |
| Rules Engine | Sandboxed CEL/decision-table evaluation against canonical inputs | Private model APIs, direct datastore reads, network calls |
| Flink jobs | Event-time aggregation, late data, checkpointed state | Synchronous model RPC for every decision in the initial design |
| Feature materializer | Idempotent committed update application | Incrementing aggregates a second time |
| Action Dispatcher | Permission checks, durable intent handling, bounded execution, acknowledgment | Treating a decision as confirmed enforcement |
| Flow Engine | Pinned state-flow instances, guard evaluation, durable timers, transition facts, emitted action commands | Live model calls to choose transitions, or direct provider side effects |
| Journal/Projector | Durable facts and eventual read models | Blocking real-time decisions on search-index availability |

The MVP may deploy Catalog, registries, and bundle compilation as one control-plane process. Product selection begins as a well-defined module over an in-memory compiled catalog snapshot; extract a remote service only if operational ownership warrants the added hop.
## Tenant-aware catalog and product selection
### Domain entities

| Entity | Meaning and key |
|---|---|
| Tenant | Authenticated security and configuration boundary, tenant_id |
| ProductDefinition | Reusable business capability, owner_scope + product_id + version |
| TenantSubscription | Entitlement to a product version, tenant_id + subscription_id + revision |
| TenantOverlay | Allowlisted changes to a pinned base, tenant_id + overlay_id + version |
| EffectiveProduct | Fully resolved immutable configuration; no inheritance traversal at runtime |
| SignalContract | Logical signal name, semantic version, value schema, meaning, constraints |
| ModelArtifact | Immutable model bytes, preprocessing, compatible input/output contracts, digests |
| ProviderBinding | Logical signal to approved provider/model version/transport/deadline |
| Ruleset | Predicates, dependencies, typed outcomes, priority, explanation templates |
| ActionPolicy | Permitted action types, adapters, authority, TTL, quotas, retry behavior |
| RuntimeBundle | Content-addressed closure of exact definitions, bindings, and effective policies |
| ActivationManifest | Tenant/environment/routing-cohort assignment to a bundle and activation epoch |

### Ownership and sharing
Platform-owned assets use a reserved owner scope such as platform; tenant-owned assets use the tenant’s identity. Sharing a product or model artifact does not share feature values, transaction histories, subscriptions, action credentials, or model-generated signals.
A tenant may discover only catalog entries visible to it. Executing a visible product additionally requires an active subscription and authorized version. Private tenant rules and datasets cannot become platform assets through an implicit fallback.
For the initial implementation, separate model artifacts can share a runtime process only when their access controls permit it. Tenant-specific preprocessing, caches, input vectors, request batching, and output routing must remain partitioned.
### Inheritance and override compilation
Proposed precedence is: platform safety constraints → product defaults → tenant allowlisted overlay → environment operational restrictions. An overlay cannot override platform prohibitions, tenant identity, provider credentials, maximum permitted action scope, or required audit behavior.
Compilation resolves the complete effective configuration and stores a digest. No recursive merge occurs on the request path. Reject unknown override paths, incompatible types, mutually exclusive settings, and unresolved feature/signal dependencies.
Pin a base product version. Publishing a new platform default must not silently change every tenant; subscriptions adopt it through explicit approval and activation. Automated adoption can be added later as an explicit policy, not an implicit behavior.
### Product selection algorithm
Authenticate the producer and derive its authorized tenant set. Bind exactly one effective tenant for this request.
Normalize event type, channel, region, and tokenized identifiers; validate mandatory routing fields.
Read the locally cached activation manifest for tenant, environment, and cohort. Pin manifest epoch and bundle id.
Evaluate routing predicates only against authorized products in the manifest: subscription status, effective dates, event type, region, and approved attributes.
Apply an explicit exclusivity group or multi-product fan-out policy. Resolve equal priority deterministically using stable product id; never iterate a hash map as policy.
Produce SelectedProduct{product_id, subscription_revision, effective_config_digest, bundle_id} and selection reasons.
Execute the selected DAGs with tenant-level concurrency/latency limits. Share a feature or signal result only if tenant, transaction revision, contract, binding, and input-snapshot digests match exactly.
If no product qualifies, return NOT_APPLICABLE or the explicit tenant fallback. Do not silently approve the transaction.
Authorization happens before business selection. A policy number, provider id, or request-body tenant_id is not sufficient proof of tenant identity.
### Isolation rules
**Keys:** Include tenant, environment, and execution mode in entity-feature keys, signal correlation, decision ids, action idempotency, caches, and replay namespaces.
**Database:** Every tenant-private table uses tenant-qualified keys; application queries and row policies enforce the same boundary. Background workers explicitly bind tenant context and must not use unrestricted cross-tenant reads for convenience.
**Kafka:** Shared regional topics are acceptable for trusted platform services; tenants must not receive direct unrestricted consumers of those topics. Externally exposed tenant streams require enforced broker/proxy authorization or dedicated topics.
**Artifacts:** Artifact paths and access checks include owner scope. A shared model never implies permission to read its training data.
**Resources:** Apply tenant quotas, maximum fan-out, queue limits, and fair scheduling. Dedicated deployments are an optional stronger isolation tier.
**Residency:** Route to an approved region before processing. Cross-region replication is configuration constrained by residency policy, not an unconditional resilience mechanism.
**Unknown tenant:** Reject, quarantine, and audit. Never default to a platform tenant.
## Signal contract and model/rule compatibility
### Canonical envelope
Use a stable Protobuf envelope for transport and a registered typed value map for extensibility. JSON below is the readable representation; Protobuf must use a oneof for primitive values so numbers and missing values are not silently coerced.
```
{
  "envelope_version": "1",
  "signal_event_id": "sig_demo_0001",
  "tenant_id": "tenant_a",
  "environment": "work",
  "mode": "LIVE",
  "transaction_id": "txn_001",
  "transaction_revision": 1,
  "decision_context_id": "ctx_001",
  "signal_name": "claim.fraud_probability",
  "contract_version": "1.1.0",
  "contract_digest": "sha256:<contract-bytes>",
  "binding_id": "claim-fraud-primary",
  "binding_version": 3,
  "producer_id": "inference-service",
  "model_id": "claim_fraud_logistic",
  "model_version": "2",
  "model_digest": "sha256:<model-bytes>",
  "preprocessing_digest": "sha256:<preprocessing-bytes>",
  "input_snapshot_digest": "sha256:<input-bytes>",
  "event_time": "2026-09-26T20:00:00Z",
  "computed_at": "2026-09-26T20:00:00.025Z",
  "expires_at": "2026-09-26T20:00:00.075Z",
  "status": "OK",
  "values": {"probability": 0.87},
  "quality": {"defaulted_features": [], "stale_features": []},
  "traceparent": "<trace-context>"
}
```
Illustrative digests and times are not executable secrets or production values. decision_context_id for synchronous requests comes from the orchestrator; external producers must return a platform-issued context token, or their outputs must be correlated through a separately validated pre-scored-event contract.
### Registered semantic contract
```
signal_name: claim.fraud_probability
version: 1.1.0
value_schema:
  probability: {type: float64, required: true, minimum: 0.0, maximum: 1.0}
semantics:
  meaning: probability_that_this_claim_is_fraudulent
  population: synthetic_claim_submissions_v1
  target_label: synthetic_fraud_label_v1
  label_observation_horizon: P30D
  unit: probability
  higher_means: more_risk
  calibration_contract: synthetic_calibration_v1
scope: transaction_revision
allowed_statuses: [OK, UNAVAILABLE, INVALID_INPUT, TIMED_OUT]
compatibility:
  additive_optional_values: allowed_after_validation
  semantic_change: new_major_required
```
The 30-day horizon here describes label observation, not signal freshness or a prediction valid for 30 days. A score for one transaction revision must never be reused for another merely because it has not expired.
### Three separate version axes
**Envelope version:** Changes to transport identity, correlation, status, and time fields. These are rare and may require client/service code changes.
**Signal contract version:** Changes to the value schema or semantic meaning. Rules declare acceptable contracts.
**Model artifact version:** Changes to weights or implementation while producing a compatible signal. Rules do not reference this version directly; the provider binding does.
Feature definitions, preprocessing, calibration, and action adapters also have independently versioned identities. The bundle binds their exact digests for execution and replay.
### Compatibility matrix

| Change | Rule source change? | Service rebuild / SQL migration? | Required gate |
|---|---|---|---|
| Threshold within allowed range | No engine change; configuration update | No | Simulation, authorization, approval |
| Rule predicate using existing registered inputs | Ruleset asset changes | No | CEL type/complexity checks and replay |
| Model weights, same supported runtime and semantic contract | No | No | Contract vectors, calibration/quality checks, rollout approval |
| Add optional signal value | Existing rules unchanged | No for generic payload storage/runtime | Schema compatibility and old-reader tests |
| Consume the new value in a rule | New rule dependency/configuration | No if type/operator already supported | Producer readiness and dependency closure |
| Change score from [0,1] to [0,100] | New contract or explicit reviewed mapping | Possibly no runtime code, but not automatically compatible | Reject old binding unless adapter declares valid semantics |
| Change label, horizon, or population meaning | Revalidate affected policies | Depends on runtime support | New semantic major and business approval |
| Introduce new inference backend | No direct model dependency in rules | Provider adapter code may be required | Runtime integration/security/performance tests |
| Add searchable field/index across payloads | No | Index/projection change may be required | Query and storage migration review |
| Add new external action provider | No scoring change | Adapter code/config/credentials may be required | Action contract and authority tests |

Use schema compatibility checks as one gate, not proof of business equivalence. Schema tooling can validate structure and configured data-quality constraints, while the platform still needs its own semantic and policy gates (Confluent stream-governance documentation).
### Signal provider abstraction
```
SignalProvider.resolve(context, binding, required_contract, deadline)
    -> ValidSignal | Missing | Invalid | TimedOut
```
Implement GrpcInferenceProvider in the MVP. Propose PreScoredEnvelopeProvider and KafkaSignalProvider as later adapters behind the same interface, subject to confirmation of actual external-producer requirements.
The resolver verifies authenticated producer authorization, tenant, environment, execution mode, transaction revision, context token, exact approved binding/model identity, input provenance, contract compatibility, quality policy, and expiry. Matching only transaction_id is insufficient.
Validate platform-issued context tokens for signature, audience, tenant, and expiry when they cross trust boundaries. Use monotonic elapsed time for local execution deadlines; compare remote timestamps only under an explicit clock-skew policy, retain trusted receipt time, and reject impossible future timestamps. A producer-provided freshness value is not independently trusted.
Contract compatibility ranges are evaluated during compilation. Runtime uses the resulting explicit set of contract digests; it does not resolve “latest” from a mutable registry on each request.
### Asynchronous score arrival, when enabled
**Before transaction:** Retain in a bounded tenant-partitioned buffer until its context can be validated; expire or quarantine orphan signals.
**After transaction:** Join only against the matching open context until its processing-time deadline. Fraud-response deadlines use elapsed time, not a Flink event-time watermark.
**Duplicate:** Deduplicate producer event id and canonical payload digest; same id with different content is a conflict.
**Multiple candidates:** Use a pinned binding and deterministic candidate-selection policy, not “last response wins.” Challenger signals are separately marked and never used for champion actions.
**After terminal decision:** Journal as late. Do not silently amend an already-issued decision or repeat its action. An authorized reassessment creates a new decision generation with explicit linkage.
**No score:** Execute declared fallback. Do not extend the deadline indefinitely because the producer is independently deployed.
## Configurable execution and worked example
### Effective product configuration
This is the resolved form executed by the runtime; catalog inheritance and authorization are already compiled. Example probabilities and thresholds are synthetic.
```
product_id: claim_decisioning
product_version: 1
tenant_id: tenant_a
subscription_revision: 4
effective_config_version: 7
runtime_profile: claim_submission
routing:
  event_types: [CLAIM_SUBMISSION]
  channels: [PORTAL]
execution:
  total_deadline_ms: 100
  required_features:
    - claimant_claim_count_1h@1
    - claimant_amount_sum_24h@1
    - provider_claim_count_1h@1
    - provider_amount_sum_1h@1
  signals:
    fraud:
      contract: claim.fraud_probability
      accepted_contracts: ["1.1.0"]
      binding: claim-fraud-primary@3
      timeout_ms: 20
      required: true
  ruleset: claim_fraud_policy@5
  missing_required_signal: REVIEW
  aggregation: claim_action_precedence@1
thresholds:
  review_probability: 0.70
  decline_probability: 0.90
  velocity_decline_count: 12
action_policy: claim_actions@2
rollout:
  mode: LIVE
  cohort: champion
```
The provider binding carries model-specific information:
```
binding_id: claim-fraud-primary
version: 3
tenant_id: tenant_a
provider: grpc_inference
endpoint_ref: inference-local
model: claim_fraud_logistic@2
model_digest: "sha256:<artifact>"
input_schema_digest: "sha256:<ordered-input-contract>"
preprocessing_digest: "sha256:<preprocessing>"
output_contract: claim.fraud_probability@1.1.0
maximum_age_ms: 50
required_quality: {allow_stale_required_features: false}
```
endpoint_ref resolves through an administrator-approved endpoint catalog. Tenant overlays cannot introduce arbitrary URLs.
### Declarative rules
```
ruleset_id: claim_fraud_policy
version: 5
requires:
  - signal: claim.fraud_probability
    contract: "1.1.0"
    alias: fraud
    optional: false
evaluation: all_match
rules:
  - id: velocity_block
    when: "features.claimant_claim_count_1h > cfg.velocity_decline_count"
    outcome: {decision: DECLINE, reason: VELOCITY_LIMIT}
    requires_signals: []
  - id: model_decline
    when: "signals.fraud.probability >= cfg.decline_probability"
    outcome: {decision: DECLINE, reason: MODEL_HIGH_RISK}
    requires_signals: [fraud]
  - id: model_review
    when: "signals.fraud.probability >= cfg.review_probability && signals.fraud.probability < cfg.decline_probability"
    outcome: {decision: REVIEW, reason: MODEL_REVIEW_BAND}
    requires_signals: [fraud]
default_outcome: APPROVE
missing_required_signal_outcome: REVIEW
```
Missing required inputs do not become CEL zero values. Evaluate signal-independent rules, mark dependent rules skipped, inject the explicit missing-signal outcome, and aggregate. The configured default outcome applies only after valid required inputs and successful evaluation, not after a timeout or engine exception.
CEL context aliases are compiled from registered schemas and feature refs. Enforce expression size, supported operators, instruction/cost limits, and permitted actions. An arbitrary expression string is not considered safe merely because it is stored as configuration.
### Two-tenant worked example
Both tenants subscribe to claim_decisioning@1 and can use the same shared model artifact. Their private effective configurations and state remain distinct.

| Item | Tenant A | Tenant B |
|---|---|---|
| Review threshold | 0.70 | 0.70 |
| Decline threshold | 0.90 | 0.85 |
| Illustrative model probability | 0.87 | 0.87 |
| Other inputs | Valid; no independent decline rule fires | Valid; no independent decline rule fires |
| Policy result | REVIEW | DECLINE |
| Authorized demo effect | Simulated response plus case intent | Simulated decline response |

The same numeric score is intentionally supplied to isolate policy behavior. The example does not imply that real tenants with different private feature histories produce equal model outputs.
Changing Tenant A’s decline threshold to 0.85 produces a new overlay/configuration/bundle and approved activation. It requires no model retraining, rules-engine rebuild, or database migration. Tenant B’s active configuration is unaffected.
### Aggregation and authority
For the demonstration, use DECLINE > REVIEW > APPROVE only within the single claim-fraud decision domain. Across multiple products, define explicit authority, exclusivity, and composition: advisory scores cannot overrule an authoritative compliance decline, and billing flags cannot determine risk policy.
The original highest-priority-enrolled-product rule is no longer an assumed universal default. Preserve it only as an optional named aggregation strategy after business confirmation. Conflicting authoritative actions produce a configured conflict outcome, never iteration-order-dependent behavior.
### Request execution
Accept and authenticate a transaction; tokenize sensitive values before broad distribution.
Derive tenant and correlation context, validate payload size/schema, and establish a monotonic deadline.
Pin activation epoch, select entitled products, and load effective configuration from local cache.
Deduplicate by tenant, mode, transaction id, and revision. A conflicting payload is rejected.
Resolve the union of compatible feature dependencies once; apply Tier 1 updates once and capture the returned vector.
Execute independent rule groups and provider calls concurrently where the DAG permits.
Validate returned signal envelopes and evaluate signal-dependent rules against canonical values.
Aggregate product outcomes under the approved policy, and construct only permitted action intents.
Commit decision, canonical feature contribution, required audit facts, action commands, and consumed input offsets through a Kafka transaction.
Return/publish the decision response; the action executor records actual effect status separately.
For direct synchronous API clients, ingress durably publishes a request and waits for a correlated result until its deadline. A timeout is a transport outcome, not proof that no decision/action occurred; provide a status lookup using the stable request identity.
## Flink and feature consistency
### Placement
Flink remains a first-class MVP component for asynchronous event-time feature computation. It does not own the synchronous Tier 1 counter namespace, and it does not execute the model-scoring RPC in the initial architecture.
```
Committed tokenized contributions
    → validate + deduplicate
    → keyBy(tenant, mode, provider, currency)
    → event-time tiles + watermark/late-data handling
    → transactional feature.updates topic
    → read_committed materializer
    → versioned Tier 2 online store
```
### Tier 1 contract
Use atomic per-entity update/dedup/returned-vector operations for decision-critical claimant count and amount velocities. For local development, implement a Redis Lua operation with all affected keys in one tenant/entity hash slot; do not use unprotected client-side read-compute-write.
The MVP defines processing-time minute-bucket windows, including the current event exactly once within a 24-hour dedup horizon. Keep a cached returned vector for retries; repeated requests do not recompute a different counter result. Reject live requests outside the accepted horizon, and run older replay fixtures in a separate namespace.
The 24-hour amount feature is scoped to claimant and currency; cross-currency totals require an explicitly versioned conversion policy. Production durability, failover, regional ownership, and recovery guarantees of the chosen Tier 1 store remain a mandatory benchmark/review gate.
### Tier 2 contract
Seed provider claim count and amount-sum features using 1-minute event-time tiles over a trailing 1-hour window, scoped by tenant, mode, provider, currency, and feature version. Proposed out-of-orderness is 2 seconds, allowed lateness 60 seconds, and source-idleness timeout 10 seconds.
Emit absolute tile values on accepted changes, not increments at the materializer. Include feature definition digest, time interval, event-time coverage, computation time, and provenance. Beyond-lateness events go to a late-data topic for investigation or isolated backfill; they do not retroactively change an already-returned decision.
The Feature Service selects tiles under the declared window-boundary convention and captures the values actually served. A collection of Tier 2 reads is not automatically a globally atomic feature snapshot. Model training and replay must respect the same convention and the captured known-as-of information.
### Recovery and materialization
Propose a 1-second checkpoint interval, 30-second checkpoint timeout, MinIO checkpoint/savepoint storage, and a compatible transactional Kafka sink. Pin connector/runtime versions and tune broker transaction timeouts against worst-case checkpoint/recovery time.
The materializer consumes committed feature updates and atomically stores the absolute tile value with its source partition/offset. Duplicate or older offsets for that key are no-ops; commit the consumer offset only after the store write. Keep output partition count fixed for the MVP; a topology change requires a new namespace and controlled rebuild.
Flink’s managed-state recovery and end-to-end sink guarantees are separate concerns; transactional sinks are needed for the relevant end-to-end guarantees (Apache Flink operations documentation). This design requires recovery tests and makes no exactly-once claim for the entire transaction-to-external-action system.
### Freshness and evolution
**Freshness target:** Proposed healthy-path p95 contribution-commit-to-visible-feature latency ≤ 5 seconds.
**Staleness policy:** Seed model permits 30 seconds of pipeline staleness; invalid required features trigger explicit fallback. A quiet entity’s old last event is not alone proof the pipeline is stale.
**Health evidence:** Track committed progress markers, input lag, watermark progress, checkpoint age, materializer lag, and per-feature coverage separately.
**No double count:** Only canonical contributions feed a given transaction aggregate. Do not also consume ingress for the same definition.
**New feature version:** Compile supported declarative operators, create an isolated state/store namespace, backfill or warm it, validate parity, then activate dependent bundles.
**No invented no-restart promise:** A feature-definition change may require a Flink job update/savepoint migration even when no application source changes.
### AI inference design
Start with a real synthetic logistic classifier exported to ONNX. Store immutable model bytes, input schema, preprocessing, output semantic contract, synthetic training provenance, and validation results in MinIO with metadata in the model registry.
### Runtime requirements
**Warm readiness:** Download and verify digests, initialize runtime, execute golden vectors, and only then advertise readiness for that model digest.
**Bounded execution:** Run scoring in a bounded CPU pool; configure ONNX and process thread limits explicitly to avoid oversubscription.
**Input validation:** Verify ordered features, types, units, missingness masks, preprocessing digest, and quality policy.
**No mutable latest:** A request selects a pinned provider binding and exact artifact. During rollout, old and new artifacts coexist until pinned in-flight work drains.
**Canonical output:** Wrap model output in the signal contract; rules never call model-specific endpoints or read model-specific tables.
**Deadline enforcement:** Respect remaining request budget, bound retries, and open a breaker on sustained provider failures. Do not retry a model call beyond the decision deadline.
**Explanation:** Persist input snapshot and identity metadata in the protected journal. Heavy attribution is asynchronous; it must not block the initial scoring path.
Weights may change without a rule release if semantic and quality gates pass. A different label definition, score interpretation, or calibration regime can invalidate thresholds despite identical numeric types, so compatibility must not be inferred from JSON shape alone.
## Real-time action execution
### Separate decision from effect
DecisionResult records what policy decided. ActionIntent records a permitted desired effect. ActionExecution records what the downstream system actually acknowledged, rejected, or left uncertain.
An inline claim response can be time-critical while case creation and notifications are asynchronous. Do not require slow secondary actions to complete before returning the transaction decision. Conversely, if a product requires a downstream acknowledgment inside its SLA, budget and test that integration explicitly.
### Action classes

| Action class | Example | Default MVP implementation |
|---|---|---|
| Inline response | Return APPROVE / DECLINE / REVIEW to caller | Local claim-response simulator |
| Asynchronous workflow | Open an SIU investigation case | Local durable case adapter |
| Notification | Emit investigation alert | Local notification sink |
| High-impact state change | Block account, modify limit | Disabled unless explicitly designed and authorized |

Rule authors select registered action types. They cannot supply raw provider URLs, credentials, arbitrary SQL, or unbounded action payloads.
Choose one enforcement route per action. An upstream system reading egress and an action adapter consuming commands must not both independently issue the same claim-response effect; configure which integration owns enforcement and use the same stable action identity for retries/status. In the local demonstration, egress exposes the decision while only the simulator adapter records the synthetic effect.
### Action policy example
```
action_policy_id: claim_actions
version: 2
tenant_id: tenant_a
allowed_actions: [CLAIM_RESPONSE, OPEN_SIU_CASE]
adapters:
  CLAIM_RESPONSE: local_claim_simulator@1
  OPEN_SIU_CASE: local_siu_case_adapter@1
authoritative_decision_domain: claim_decisioning
intent_ttl_ms:
  CLAIM_RESPONSE: 100
  OPEN_SIU_CASE: 300000
retries:
  OPEN_SIU_CASE: {maximum_attempts: 3, strategy: bounded_exponential}
on_unknown_outcome: RECONCILE
live_effects_allowed: false
```
TTL is measured against the relevant original request/intent time, not reset on every retry. The 100 ms response example is a proposed local target and must not be mistaken for permission to issue a delayed real claim response.
### Durable dispatcher protocol
Consume a committed action command and verify tenant, mode, intent expiry, approved adapter, payload schema, and action policy.
Derive the idempotency key from (tenant, environment, decision_id, decision_generation, action_type, target). Do not include retry attempt; retries must reuse the same key.
In one local Postgres transaction, insert inbox/dedup record and action ledger row. Reject payload-hash conflicts.
Claim the action with a lease and fencing generation; only the current generation may update terminal state.
Send the idempotency key to adapters that support it. Persist request/provider references without secrets.
On definitive acknowledgment, mark ACKNOWLEDGED; on definitive rejection, mark FAILED.
On timeout after possible dispatch, mark UNKNOWN and query/reconcile provider status. Retry only when the adapter’s idempotency and outcome contract makes it safe.
Publish ledger transitions through an outbox committed with each state change; relay retries are deduplicated downstream.
Proposed state machine:
```
PENDING → DISPATCHING → ACKNOWLEDGED
                    → FAILED
                    → UNKNOWN → RECONCILING → ACKNOWLEDGED | FAILED | MANUAL_REVIEW
PENDING → EXPIRED | CANCELLED
```
Cancellation after dispatch is best effort and cannot erase an effect. A lease alone does not prevent duplicate external execution after a stalled worker resumes; provider-side idempotency/fencing or reconciliation is still required. Providers without those capabilities must not be described as exactly-once.
### Reassessment and kill switches
Final decisions are immutable facts. A reassessment creates a new generation linked to the earlier decision; an explicit policy determines whether any new action is authorized or compensation is required.
Replay, shadow, and dry-run modes prohibit live dispatch at both the orchestrator and adapter boundaries. Tenant/environment/global kill switches can block new intents or dispatch while allowing read-only diagnosis. Check current emergency authorization at dispatch in addition to the policy pinned at decision time.
## Declarative state flows and model-assisted authoring
Some products need multi-step state beyond a single decision: review-case lifecycle, step-up authentication, hold-and-release, and reassessment. Model these as declarative state-flow assets compiled into the runtime bundle, the same way rulesets are. Do not implement them as product-specific code branches, and do not let a live model call choose the next state.
A language model may help author, test, and operate these flows. It may not execute them. The flow engine that applies transitions is deterministic, pinned to a bundle digest, and replayable.
### State-flow asset
A state flow declares states, transitions, guards, timers, and the registered action types each transition may emit. Guards are CEL expressions evaluated under the same sandbox and cost limits as rules. The example below is synthetic.
```
flow_id: siu_case
version: 1
kind: state_flow
subject: decision   # key: tenant, env, mode, flow, decision, generation
initial: OPEN
states:
  OPEN:            {terminal: false, timer: {id: sla_open, after: PT4H}}
  IN_REVIEW:       {terminal: false, timer: {id: sla_review, after: PT24H}}
  ESCALATED:       {terminal: false, timer: {id: sla_escalated, after: PT8H}}
  CONFIRMED_FRAUD: {terminal: true}
  CLEARED:         {terminal: true}
  EXPIRED:         {terminal: true}
transitions:
  - id: assign
    from: [OPEN]
    to: IN_REVIEW
    on: event.case_assigned@1
    guard: "actor.role == 'investigator'"
  - id: escalate
    from: [IN_REVIEW]
    to: ESCALATED
    on: event.escalation_requested@1
    guard: "signals.case_triage.priority >= cfg.escalation_priority"
    requires_signals: [case_triage]
    on_missing_signal: HOLD
  - id: confirm
    from: [IN_REVIEW, ESCALATED]
    to: CONFIRMED_FRAUD
    on: event.disposition_submitted@1
    priority: 10
    guard: "input.disposition == 'FRAUD' && actor.role in ['investigator', 'senior_investigator']"
    emits: [NOTIFY_INVESTIGATION]
  - id: clear
    from: [IN_REVIEW, ESCALATED]
    to: CLEARED
    on: event.disposition_submitted@1
    priority: 20
    guard: "input.disposition == 'NOT_FRAUD'"
  - id: expire
    from: [OPEN, IN_REVIEW, ESCALATED]
    to: EXPIRED
    on: timer.fired
    guard: "timer.id.startsWith('sla_')"
    emits: [NOTIFY_OPERATIONS]
action_policy: siu_case_actions@1
```
emits names registered action types only. The compiler rejects any action not permitted by the pinned action policy, and emitted commands follow the durable dispatcher protocol unchanged.
### Flow engine runtime rules
Key every flow instance by tenant, environment, mode, flow id, and subject identity. A new instance pins the flow version and bundle digest at creation.
Consume a transition event through the inbox, deduplicating by tenant and event id. The same id with a different payload is a conflict.
Select candidate transitions whose from includes the current state and whose on matches the event type and contract version.
Evaluate guards against canonical inputs. A guard whose required signal is missing, invalid, or expired never evaluates to false by default; apply its declared on_missing_signal behavior (HOLD or a named fallback transition).
Apply exactly one transition. When several share a source state and event, the compiler requires distinct explicit priorities; if more than one guard still matches at equal priority, reject with FLOW_CONFLICT and record it. Never use declaration or iteration order as policy.
In one Postgres transaction, update the instance with optimistic concurrency on its state version, append the transition fact, and write emitted action commands to the outbox.
Schedule declared timers durably. A timer firing is an event with a stable id, so a restart cannot fire it twice.
In-flight instances continue on their pinned flow version when a new version is activated. Moving live instances to a new version requires an approved migration asset that maps old states to new ones; it is never implicit. Replay and shadow instances run in isolated namespaces and cannot emit live actions.
### Flow compilation checks
**Reachability:** every state is reachable from initial, and every non-terminal state has at least one exit through an event or timer.
**Terminal states:** no transition may leave a terminal state.
**No automatic loops:** any cycle must include an external event or a timer; guard-only cycles are rejected.
**Types:** guards type-check against registered event, signal, actor, and configuration schemas.
**Authority:** emitted actions are permitted by the pinned action policy, and transitions requiring human roles declare them.
**Budgets:** guard expression size and cost stay within limits, and every non-terminal state declares an SLA timer unless an approved exemption exists.
### Where language models participate
Language models, large or small, operate around the decision path, not inside its authority. The governing pattern: a model may produce contracted signals or propose drafts. Only the compiled, pinned bundle decides and transitions state, and only the dispatcher acts under action policy.

| Role | Placement | Authority | Boundary |
|---|---|---|---|
| Signal provider | Classify provider descriptors, free-text fields, and case notes into registered signals such as provider.category_risk or case_triage.priority | Produces evidence only | Behind SignalProvider with a semantic contract, binding, fallback, and shadow evaluation; precomputed or asynchronous by default |
| Case assistant | Summarize a review case and suggest a disposition | Advisory | Runs after the case action is acknowledged; a human submits the disposition event |
| Explanation writer | Plain-language narrative from fired rules and the signal snapshot | Advisory | Asynchronous from decision_fact; code-generated reason codes remain authoritative |
| Authoring assistant | Draft rules, overlays, thresholds, and state flows from feedback, replay diffs, and incident notes | Draft only | Enters as a DRAFT asset; normal compile, simulation, replay diff, and separate human approval |
| Operations triage | Cluster UNKNOWN actions and stuck flow instances; suggest reconciliation steps | Advisory | Reconciliation logic stays deterministic; suggestions require an authorized operator |

### Controls for model participation
**Contracts first:** any model output consumed by a rule or guard is a registered signal contract with meaning, unit, allowed statuses, and a declared fallback, exactly like model scores.
**Draft provenance:** model-assisted assets carry origin: model_assisted plus model id, version, prompt-template digest, and input-reference digests. Approval binds the immutable content, not the prompt.
**No approval by a model:** model service identities cannot hold approver, activation, or action-administrator roles. Separation of duties applies to the human who submits a model-assisted draft.
**No write path to authority:** model service identities have no write access to activations, flow instances, action commands, or the action ledger. Enforce with RBAC and database roles, not convention.
**Untrusted input:** transaction and case text is data, never instruction. Tokenize or redact sensitive fields before a model sees them, validate outputs against a schema, and keep prompt-injection fixtures in the test suite.
**Reproducibility:** journal the model identity, prompt-template digest, parameters, and output. Decisions and transitions never depend on re-running a model during replay.
**Latency:** language-model calls are excluded from the 100 ms synchronous path by default. A small model may be bound as a synchronous signal provider only after it meets the inference budget at p99 under the load test, with a declared fallback.
### Delivery placement and acceptance tests
Build the flow engine in Phase 2, with the review-case flow as its first asset. Model-backed signal providers follow the Phase 3 transport work. Case assistant, explanation, authoring, and triage assistants belong in Phase 4.

| Gate | Pass condition |
|---|---|
| Flow compilation | Unreachable state, exit from a terminal state, guard-only cycle, unpermitted action, and missing SLA timer are each rejected with a structured diagnostic |
| Pinned flow version | Activating flow v2 leaves in-flight v1 instances on v1; only an approved migration moves them |
| Transition idempotency | A duplicate event produces one transition and one set of action commands |
| Guard ambiguity | Two matching guards at equal priority produce FLOW_CONFLICT and no transition |
| Missing guard signal | A missing or expired signal produces the declared HOLD or fallback, never an implicit false |
| Durable timers | An SLA timer fires exactly once across a scheduler restart |
| Model authority | A model service identity cannot write activations, flow instances, action commands, or the action ledger |
| Model-assisted draft | A model-assisted asset cannot activate without separate human approval, and its provenance is recorded |
| Prompt injection | Malicious provider descriptors and case notes do not change signal schemas, flow transitions, or permitted actions |
| Replay isolation | Flows replayed or shadowed emit zero live actions |

## Persistence and data ownership
### Stable envelope plus validated payload
Postgres stores control metadata, projections, and the action ledger; immutable artifacts live in object storage; Kafka carries durable facts. Use JSONB for extensible definitions and payloads, but validate them against registered schemas before writing.
Do not create a table per model, a column per score, or a rules-engine query against a model’s private database. Equally, do not claim JSONB eliminates every migration: new indexed access paths, envelope fields, retention layouts, and partitioning strategies still require planned changes.
### Logical tables

| Table | Primary identity | Important constraints |
|---|---|---|
| tenant | tenant_id | status, region policy, quotas |
| asset_version | owner_scope, kind, asset_id, version | immutable spec/hash; explicit lifecycle metadata |
| subscription | tenant_id, subscription_id, revision | pinned product version and entitlement window |
| tenant_overlay | tenant_id, overlay_id, version | allowed override paths only |
| activation | tenant_id, environment, cohort, epoch | optimistic concurrency; immutable history |
| signal_event | tenant_id, mode, signal_event_id | immutable normalized envelope; payload hash conflict check |
| decision_fact | tenant_id, mode, decision_id, generation | pinned manifest and exact input/output snapshots |
| execution_event | tenant_id, mode, event_id | append-only lifecycle/audit facts |
| action_execution | tenant_id, environment, idempotency_key | mutable state projection plus append-only transitions |
| flow_instance | tenant_id, environment, mode, flow_id, subject_id | pinned flow version and bundle digest; state version for optimistic concurrency |
| flow_transition | tenant_id, mode, instance key, transition_seq | append-only transition facts with event id and guard inputs |
| inbox | consumer_name, tenant_id, message_id | duplicate suppression and payload hash |
| outbox | outbox_id | transactional insertion with local state change |

### Representative Postgres DDL
This is a starting migration for key boundaries, not the complete set of control-plane tables. Use unpartitioned tables in the MVP; introduce partitioning with compatible keys and foreign-key strategy later.
```
CREATE TABLE asset_version (
  owner_scope text NOT NULL,
  kind text NOT NULL,
  asset_id text NOT NULL,
  version text NOT NULL,
  spec jsonb NOT NULL,
  content_digest text NOT NULL,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_scope, kind, asset_id, version)
);

CREATE TABLE decision_fact (
  tenant_id text NOT NULL,
  mode text NOT NULL CHECK (mode IN ('LIVE','SHADOW','REPLAY')),
  decision_id text NOT NULL,
  generation integer NOT NULL CHECK (generation > 0),
  transaction_id text NOT NULL,
  transaction_revision integer NOT NULL,
  bundle_digest text NOT NULL,
  manifest_epoch bigint NOT NULL,
  decided_at timestamptz NOT NULL,
  outcome text NOT NULL,
  input_snapshot jsonb NOT NULL,
  signal_snapshot jsonb NOT NULL,
  result jsonb NOT NULL,
  payload_digest text NOT NULL,
  PRIMARY KEY (tenant_id, mode, decision_id, generation)
);

CREATE TABLE action_execution (
  tenant_id text NOT NULL,
  environment text NOT NULL,
  idempotency_key text NOT NULL,
  decision_id text NOT NULL,
  decision_generation integer NOT NULL,
  action_type text NOT NULL,
  intent jsonb NOT NULL,
  payload_digest text NOT NULL,
  state text NOT NULL,
  lease_generation bigint NOT NULL DEFAULT 0,
  lease_expires_at timestamptz,
  provider_reference text,
  expires_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, environment, idempotency_key)
);

CREATE TABLE outbox (
  outbox_id uuid PRIMARY KEY,
  tenant_id text NOT NULL,
  topic text NOT NULL,
  message_key text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz
);

ALTER TABLE decision_fact ENABLE ROW LEVEL SECURITY;
ALTER TABLE decision_fact FORCE ROW LEVEL SECURITY;
CREATE POLICY decision_tenant_policy ON decision_fact
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
```
Apply equivalent tenant policies to every tenant-private table; the single policy shown is not a complete isolation implementation. Set tenant context transaction-locally from authenticated server context using a non-owner, non-bypass runtime role. Platform-shared asset access uses a separate explicit authorization path, not a tenant-policy exception that grants broad reads.
Decision-fact writes are append-only and idempotent. Lifecycle/status projections may change, but their updates must not overwrite original decision snapshots. Payload retention, encryption, redaction, legal hold, and deletion obligations require explicit production policy.
## APIs and messaging
### Control-plane API

| Method/path | Operation | Required checks |
|---|---|---|
| POST /v1/products | Create a product definition | Owner permission; schema validation |
| POST /v1/tenants/{tenant}/subscriptions | Subscribe/pin product | Tenant admin; entitlement |
| POST /v1/tenants/{tenant}/overlays | Propose override | Allowlist and safety constraints |
| POST /v1/assets/{kind}/{id}/versions | Add immutable version | Owner scope; content validation |
| POST /v1/bundles/validate | Compile closure and return diagnostics | Tenant-scoped dependencies |
| POST /v1/bundles/{digest}/approve | Approve candidate | Role separation; artifact unchanged |
| POST /v1/activations | Activate bundle for tenant/env/cohort | Expected epoch, readiness, authority |
| POST /v1/simulations | Evaluate historical/synthetic fixture | Isolated mode; no live actions |
| GET /v1/decisions/{id} | Authorized trace/status lookup | Tenant scope; redaction |
| GET /v1/actions/{key} | Action state and provider status | Tenant scope; protected details |

Use Idempotency-Key for mutations and an expected revision/epoch for concurrent updates. Stale writes return a conflict; validation failures return structured dependency/type/permission diagnostics. Never auto-upgrade dependencies to make validation pass.
### Runtime RPCs
```
Decide(AuthenticatedTransaction, request_deadline)
SelectProducts(TenantContext, RoutingAttributes, pinned_manifest)
ResolveFeatures(DecisionContext, versioned_feature_refs, update_policy)
ResolveSignals(DecisionContext, approved_bindings, deadline)
Score(ModelBinding, InputVector, InputSnapshotDigest, deadline)
EvaluateRules(RulesetDigest, CanonicalContext, execution_budget)
GetDecisionStatus(TenantContext, stable_request_id)
```
RPC messages carry tenant, environment, mode, transaction revision, context identity, manifest epoch, bundle digest, and trace context where applicable. Internal callers cannot override authenticated tenant bindings. Heavy model payloads and feature vectors must have explicit size limits.
### Topic contracts

| Topic | Producer → consumer | Key / semantics |
|---|---|---|
| rtdp.ingress.v1 | trusted ingress → orchestrator | tenant + mode + tokenized entity |
| rtdp.egress.v1 | orchestrator → upstream/status projector | tenant + transaction revision |
| rtdp.feature.contrib.v1 | orchestrator → Flink | one canonical tokenized contribution per accepted event |
| rtdp.feature.updates.v1 | Flink → materializer | tenant + mode + entity + currency + feature version + tile |
| rtdp.feature.late.v1 | Flink → investigation/backfill | tenant + source event |
| rtdp.signals.v1 | authorized external provider → signal adapter | proposed later transport; tenant + context + signal |
| rtdp.decision.facts.v1 | orchestrator → journal/projector | immutable terminal decision and protected snapshot |
| rtdp.action.commands.v1 | orchestrator → action dispatcher | tenant + action idempotency key |
| rtdp.action.status.v1 | action outbox → projectors | tenant + action key + state revision |
| rtdp.control.activation.v1 | coordinator → runtimes | tenant + environment + cohort; compacted |
| rtdp.telemetry.v1 | services → observability | operational events; not sole audit source |
| rtdp.dlq.v1 | consumers → operations | protected original envelope + typed failure |

All topic headers include schema identity and trace context. Partition counts and retention are workload decisions, not hard-coded production facts. Keep messages with the same materialized-key identity ordered within the declared topic topology.
### Commit boundaries
The Kafka ingress adapter transactionally publishes terminal decision, required audit snapshot, action intents, canonical contribution, and input offset on the same broker cluster. Consumers requiring committed results use read_committed; telemetry outside that transaction must not be the only record of a consequential decision.
This transaction does not include Redis, Postgres, remote inference, or a provider’s side effect. Tier 1 updates require independent atomic deduplication; projectors and action consumers require inbox/idempotency checks. Record an event contribution id independent of product fan-out so multiple products do not inflate feature counts.
The orchestrator may repeat model computation after a crash before commit. Only committed terminal decisions authorize actions; consumers deduplicate logical decision/action identities. Do not promise deterministic recovery from fresh feature reads: preserve or reconstruct the pinned input vector/configuration before re-executing.
## Asset lifecycle and independent activation
### Lifecycle
```
DRAFT → VALIDATED → APPROVED → PREPARED → ACTIVE → RETIRED
            |                     |
          REJECTED              ABORTED
```
Artifact content is immutable after version creation; lifecycle events and approval records are stored separately. Any edit to content creates a new version and invalidates approval for the old digest.
### Compile and validate
Resolve tenant entitlement and a pinned product base.
Apply only permitted overlays; produce a fully materialized effective product.
Build dependencies across flows, features, signals, bindings, models, rules, aggregation, and action policies.
Reject cycles, unresolved refs, invalid types, incompatible semantics, unauthorized assets, and exceeded execution budgets.
Check contracts against golden vectors, old consumer behavior, and replay fixtures.
Verify required feature version readiness, artifact availability, and action adapter support.
Write a canonical manifest with exact digests, compiler version, validation evidence, and required runtime capabilities.
Sign manifest content excluding its own signature/digest fields; never create a self-referential hash.
### Prepare, then activate
Runtimes preload and verify candidate artifacts and advertise readiness by digest and capability. The coordinator updates the tenant/environment/cohort manifest only when eligible runtime pools are ready; requests then pin the new epoch.
During propagation, a request explicitly asks for its pinned versions. A runtime that lacks a version rejects/reroutes/falls back according to policy; it must not substitute its own current default. Keep the previous bundle and artifact pools available until outstanding requests drain.
Independent deployment is preserved: a model candidate can be loaded without modifying rules; activation can update only a provider binding. The bundle is a consistent configuration snapshot, not a requirement to rebuild every service together.
### Rollout and rollback
**Shadow:** Evaluate candidate outputs without effects, journal differences, enforce separate namespaces and quotas.
**Canary:** Use a deterministic hash of tenant, transaction/entity key, and rollout salt for stable cohorts.
**Promotion:** Require compatible signals plus approved decision/action-difference and latency evidence; numeric schema compatibility alone is insufficient.
**Rollback:** Activate the previous manifest epoch’s bundle by a new epoch. Do not edit history or assume rollback undoes actions already acknowledged.
**Emergency stop:** Disable permitted dispatch scope without waiting for a model rollback; audit who changed it and why.
## Reliability, latency, and observability
### Proposed engineering budgets

| Stage | Allocation |
|---|---|
| Ingress, tenant authentication, tokenization | 10 ms |
| Cached product selection and plan resolution | 3 ms |
| Feature update/read and snapshot | 10 ms |
| Inference including signal validation | 20 ms |
| Rules and aggregation | 10 ms |
| Durable Kafka publication and response handoff | 12 ms |
| Queueing, network, scheduling, headroom | 35 ms |
| Total proposed decision target | 100 ms |

These are allocations, not measured percentiles or proof that component p99 values add to an end-to-end p99. The initial load test is 500 TPS for five minutes after warmup on recorded hardware. A 100,000 TPS experiment is a later scale milestone, not a demonstrated capability.
Measure full ingress-to-egress latency including queueing and durable commit. External-action acknowledgment has a separate per-adapter SLO; do not include an unbounded external integration under the 100 ms claim.
### Failure behavior

| Failure | Required behavior |
|---|---|
| Control plane unavailable | Use approved cached manifests; reject new activations |
| Unknown tenant or unauthorized product | Reject, audit, no fallback to another tenant |
| Required signal absent/invalid/expired | Execute explicit missing-signal branch by deadline |
| Inference unavailable | Breaker + declared rules-only policy; no random/zero score |
| Flink stalled | Detect pipeline staleness; apply feature quality/fallback policy |
| Tier 1 update unavailable | Do not claim a valid counter; apply product fallback or fail transport |
| Kafka commit uncertain | Abort/reconcile/replay; never assert successful publication prematurely |
| Projector/search unavailable | Decisions continue if authoritative facts can commit; projections catch up |
| Action provider times out | Mark UNKNOWN, reconcile, no blind duplicate action |
| Candidate model not warm | Do not activate that binding on the unready pool |
| Tenant exceeds quota | Bound/reject tenant workload without starving other tenants |

### Metrics and evidence
Record per-service decision and inference latency; Kafka lag/commit errors; Flink checkpoint duration/age/backpressure/watermarks; feature freshness and defaults; contract rejection reasons; bundle/model versions; rules hit rates; and action outcomes including UNKNOWN duration.
Use bounded metric labels. Do not place unrestricted tenant ids, transaction ids, rule-expression text, or model payloads into high-cardinality public metrics; retain scoped details in authorized traces/logs. Propagate trace ids through Kafka and gRPC and redact sensitive fields.
A reproducible decision trace includes authenticated tenant, subscription revision, selection reasons, manifest epoch, bundle digest, feature snapshot, signal provenance/quality, rules fired/skipped, aggregation policy, and action intent/status references.
## Security and governance
**Identity:** Bind tenant from verified service/user identity; use scoped credentials and short-lived tokens for internal callers.
**Sensitive inputs:** Tokenize raw identifiers at trusted ingress; no real policyholder identifiers in the reference implementation. Tokenization scheme/key rotation must preserve intended entity continuity through explicit migration.
**RBAC:** Distinguish platform author, tenant author, approver, operator, investigator, and action administrator.
**Separation of duties:** Author cannot approve their own production asset; approval binds immutable content and effective scope.
**Untrusted artifacts:** Validate formats and model loader behavior; restrict execution/runtime operators, file access, network egress, CPU, and memory.
**Action authority:** A model or rule author cannot grant new provider credentials or permissions through configuration.
**Secrets:** Store references in config, values in an approved secret store; never commit credentials or keys to the repository.
**Supply chain:** Pin dependencies/images, scan and sign artifacts, attach SBOMs, and verify before deployment.
**Residency/retention:** Document per-tenant policies and enforce them on topics, stores, replay datasets, backups, and observability exports.
**Audit:** Protect immutable facts from ordinary application update/delete permissions and independently monitor privileged operations.
The demo does not establish PCI or any other certification. Compliance requirements and legal retention rules must be provided and reviewed for the actual deployment.
## Implementation stack and repository
### Reference choices
Use Go for orchestration, feature serving, signal resolution, CEL rules, and action workers; Python for ONNX inference and control APIs; Java for Flink jobs; React/TypeScript for authoring and investigation UI. These are implementation choices, not mandatory characteristics of the architecture.
Use a local Kafka-compatible broker, Postgres, Redis, MinIO, and OpenTelemetry/Prometheus/Grafana. Choose and lock mutually compatible maintained versions during bootstrap, including Flink connectors, Java runtime, Protobuf generation, ONNX export/runtime, and MinIO filesystem plugins; do not reuse old version pins blindly.
```
rtdp/
  AGENTS.md
  Makefile
  docker-compose.yml
  proto/rtdp/v1/
  contracts/                         # envelopes, signal semantics, feature/action schemas
  services/
    ingress/
    control-plane/                   # catalog, subscriptions, registries, compiler, approvals
    orchestrator/                    # product selection, DAG, aggregation
    feature-service/
    signal-resolver/
    rules-service/
    inference-service/
    feature-materializer/
    action-dispatcher/
    projector/
    event-api/
  adapters/
    signals/                         # grpc now; pre-scored/Kafka later
    actions/                         # local auth/case simulators first
  streaming/flink-features/
  ml/seed-model/
  assets/seed/tenants/{tenant_a,tenant_b}/
  tests/{contracts,isolation,recovery,e2e,performance}/
  tools/{harness,replay}/
  ui/
  deploy/{helm,argocd}/
  observability/
  docs/{design.md,rtdp-architecture.excalidraw,adr/,validation/}
```
Logical modules need not each be a separate container at first. Preserve contracts and isolation, but avoid turning every registry entity into a network service.
### Local startup and Kubernetes
Default Docker Compose includes real Flink JobManager/TaskManager, the real ONNX service, broker, materializer, stores, decision path, action simulators, and observability. No optional profile may quietly replace required inference/streaming with stubs.
make seed creates topics/buckets, synthetic tenants/subscriptions/configurations, four feature definitions, a deterministic model fixture, action simulator policies, and compiled bundles. It submits the Flink job, warms inference, and checks readiness before the harness starts.
Use layered Helm values for base, platform, region, and environment. ArgoCD remains the sole Kubernetes deployment controller; CI builds/signs images and proposes digest updates rather than also applying workloads. Keep application image rollout separate from business-asset activation.
## Delivery plan and acceptance tests
### Phase 0: Contracts and foundation
Implement the monorepo, schema validation, Go/Python/Java Protobuf code generation, local infrastructure, dependency locking, CI, tenant identity fixture, and immutable artifact bootstrap. Establish the contract registry and canonical typed-value envelope before implementing model-specific logic.
Acceptance: make up, make proto, and schema tests pass; Flink and inference infrastructure are included; invalid contract and cross-tenant fixture tests fail as expected.
### Phase 1: Real streaming, scoring, rules, and simulated actions
Implement two synthetic tenants and pinned static configurations; tenant-aware selection; Tier 1 atomic velocities; real Flink provider features and materializer; real ONNX logistic scoring; canonical signal validation; CEL rules; configurable aggregation; durable decision/action publication; action simulators with an idempotency ledger; and a basic trace UI.
Use static asset files with a compile/activate CLI initially. Configuration-only deployment and isolation are MVP requirements; a full graphical authoring/approval experience is not.

| Gate | Pass condition |
|---|---|
| Functional path | Real transaction → real features → real model → rules → decision → simulated acknowledged action |
| Configuration-only threshold change | New tenant threshold changes expected outcome without service rebuild, image digest change, or SQL migration |
| Compatible model swap | New model artifact and binding work with unchanged rule source and database schema |
| Optional signal evolution | Optional value accepted by old rules; a new rule can consume it after dependency validation |
| Semantic rejection | Wrong unit, incompatible major, changed meaning, or unauthorized producer rejected |
| Tenant isolation | Same transaction/entity ids under A/B produce independent state, selection, traces, and actions |
| Model parity | Native/exported logistic probability difference ≤ 1e-5 for fixed vectors |
| Streaming correctness | Ordered, duplicated, disordered, allowed-late, beyond-lateness, idle-partition, and currency fixtures match golden totals |
| Recovery | TaskManager restore and materializer crash after write-before-offset do not inflate final totals |
| Freshness | Healthy-path p95 committed-contribution-to-feature visibility ≤ 5 seconds; stopped pipeline triggers declared staleness policy |
| Inference failure | Timeout/unavailable/invalid response returns configured degraded result by deadline, not fabricated score |
| Action idempotency | Duplicate command results in one acknowledged simulated effect |
| Action ambiguity | Provider timeout after application produces UNKNOWN then reconciliation, not immediate blind retry |
| Replay/shadow | Zero live effects; isolated feature state and outputs |
| Load | 500 TPS × 5 minutes = 150,000 unique requests/results after drain; report duplicates, missing ids, p50/p95/p99/p99.99 |
| Latency | Proposed ingress-to-egress p99 < 100 ms on recorded hardware; failures disclosed without weakening the test |

### Phase 2: Governed self-service control plane
Build tenant/catalog/subscription/overlay UI, registry browsing, rule authoring, compatibility diagnostics, simulation reports, approvals, prepared artifact readiness, canary activation, rollback, and immutable audit history.
Acceptance: author and separate approver activate a tenant-specific configuration with no application rebuild; other tenant behavior is unchanged; pinned in-flight requests use one consistent bundle during rollout.
### Phase 3: Additional signal transports and model families
After confirming producer requirements, add pre-scored and Kafka signal adapters, bounded correlation, late/duplicate/orphan handling, XGBoost/MLP export parity, calibration metadata, and asynchronous attribution. Extend feature jobs with safe namespace/backfill/savepoint upgrades and benchmark replica hedging only against independent replicas.
Acceptance: the same rule source handles equivalent validated synchronous and streamed signals; mismatched tenant, revision, provenance, contract, or producer is rejected; late scores do not trigger duplicate actions.
### Phase 4: Analytics and feedback
Add lakehouse materialization, point-in-time training datasets, replay/counterfactual comparison, challenger evaluation, and investigative workflows. Recommendations create drafts; they do not bypass approvals or change production rules automatically.
Acceptance: replay uses captured or historically correct inputs, produces decision/action diffs, and cannot mutate live state. Demonstrate feedback-to-draft-to-approved-activation lineage.
### Phase 5: Production hardening and scale
Add approved production stores/adapters, Kubernetes deployment, tenant resource isolation, residency controls, disaster recovery, real enforcement integration behind explicit authorization, and staged scale experiments toward 100,000 TPS.
Acceptance: platform-specific failure tests, capacity results, dependency RTO/RPO, security review, and business-approved selection/aggregation/action semantics. Synthetic demo success is not production certification.
## Commands and evidence
```
make up
make proto
make seed
make lint test
make test-contracts
make test-isolation
make test-streaming
make test-model
make test-actions
make test-resilience
make e2e
make load TPS=500 DURATION=5m
```
Store commands, hardware, versions, image/model/manifest digests, migration history, workload distribution, raw counts, latency/freshness percentiles, and failure logs in docs/validation/. The configuration-only tests must compare image digests and database migration versions before/after, not merely claim “no code changed.”
## Open decisions and required context
**Tenant hierarchy:** Flat versus parent/sub-tenant model, delegated administration, and cross-tenant sharing authority.
**Product semantics:** Catalog metadata, entitlement/enrollment distinction, selection predicates, exclusivity, and product dependency rules.
**Aggregation:** Which product is authoritative for which action domain and how conflicts must resolve.
**Signal sources:** Whether the first integration calls models, receives pre-scored events, waits for streamed signals, or uses a combination.
**Model meaning:** Real label definition, horizon, calibration expectations, population, and acceptable quality drift.
**Action authority:** Who actually enforces approval/decline, supports step-up, creates cases, blocks accounts, or changes limits.
**Failure policies:** Per-tenant/product behavior for unavailable data/models, delayed publication, and unknown action outcome.
**Operational requirements:** Load, residency, retention, recovery objectives, isolation level, and supported platforms.
Until these are supplied, implement the marked synthetic defaults, not invented business facts. Changes affecting live authority, data sharing, or fail-open/fail-closed semantics require explicit decisions rather than an agent silently choosing the simplest option.
## Architecture decisions to record

| ADR | Decision |
|---|---|
| ADR-001 | Models and rules communicate through versioned semantic signals |
| ADR-002 | Stable envelope plus validated extensible payloads; no per-model tables |
| ADR-003 | Tenant-qualified catalog, state, correlation, and action identities |
| ADR-004 | Immutable effective bundles and request-pinned activation epochs |
| ADR-005 | Synchronous Tier 1 with asynchronous Flink Tier 2 |
| ADR-006 | Inference transport behind a provider interface, gRPC first |
| ADR-007 | Decision facts, action intent, and acknowledged effect are separate |
| ADR-008 | Explicit boundaries for Kafka transactions, store dedup, and provider idempotency |
| ADR-009 | Configuration-only changes have a supported-capability boundary |
| ADR-010 | Replay/shadow cannot perform live effects |
| ADR-011 | Language models act only as contracted signal providers or advisory, draft-only assistants; they never decide outcomes, transition state, or dispatch actions |
| ADR-012 | Multi-step state is modeled as declarative, versioned state-flow assets executed by a deterministic, pinned flow engine |

## Devin starter prompt
Read docs/design.md version 2.1, root AGENTS.md, and docs/rtdp-architecture.excalidraw. Implement Phases 0 and 1 in order. The governing requirement is independent configuration deployment: routine model, ruleset, threshold, and product-binding changes must not require application rebuilds or database migrations. Build tenant-aware catalog/selection using two synthetic tenants, a stable typed signal envelope and semantic contract registry, real Flink feature computation, real ONNX inference, declarative CEL rules, immutable effective bundles, and simulated real-time actions with durable idempotency and acknowledgment tracking. Use the proposed synthetic defaults where explicitly marked; do not assume real business entitlements or action authority. Keep synchronous scoring behind the SignalProvider interface; external score transports are Phase 3 unless requested. Enable real streaming and scoring by default in Docker Compose. Run every Phase 1 acceptance gate, including configuration-only threshold/model changes, tenant isolation, semantic incompatibility rejection, streaming recovery, model parity, action ambiguity, and 500 TPS for five minutes. Save honest evidence in docs/validation/; do not weaken tests or substitute stubs. Stop before using real customer data, real action providers, or unresolved production authority. Declarative state flows are Phase 2 and language-model assistants are Phase 4; in Phases 0 and 1, grant no model service identity write, activation, or dispatch authority (ADR-011).
