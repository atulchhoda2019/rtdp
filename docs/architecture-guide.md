<!--Converted from RTDP_v2.1_Architecture_Design_Guide_1.docx — a narrative
walkthrough of the seven views in docs/rtdp-architecture.excalidraw.
Companion to: design.md (normative spec), detailed-design.md (as-built),
aws-architecture.md (as-deployed on AWS). -->

# RTDP v2.1 — Architecture design guide

Configurable Insurance Decisioning

Architecture design guide: a walkthrough of the seven architecture views

Contents

1. Purpose and how to read this guide

2. The problem RTDP solves

3. Design principles at a glance

4. Glossary

5. View 1: Configurable decisioning architecture

6. View 2: Independent evolution through signal contracts

7. View 3: Tenant-aware catalog and product selection

8. View 4: Real Flink and AI inference in the MVP

9. View 5: Authorized actions and acknowledged effects

10. View 6: Validated configuration and independent activation

11. View 7: Delivery plan and proof

12. End-to-end walkthrough: one synthetic claim

13. Architecture decision records

14. Open questions and risks

15. Explaining RTDP in conversation

## 1. Purpose and how to read this guide

RTDP (Real-Time Decisioning Platform) is a multi-tenant platform for insurance decisions such as claim triage, underwriting and risk pricing. The architecture diagram has seven views. Chapters 5 to 11 cover one view each, in the same order, with the diagram panel at the top of the chapter. Chapters 2 to 4 set up the problem, the principles and the vocabulary.

Every chapter follows the same pattern: the idea in one line, the components and their jobs, the reasoning behind the design choices, and the invariants that must stay true. Chapter 12 then follows a single synthetic claim through the whole system end to end, and the closing chapters list the architecture decisions, the open questions, and short talking points.

Reading time is about 40 minutes. If you only have ten, read chapter 2, the principles in chapter 3, and the walkthrough in chapter 12.

## 2. The problem RTDP solves

Insurance decision logic changes all the time. Thresholds move, models are retrained, new products launch, and individual clients (tenants) want their own variations. In most organizations each of those changes becomes a coordinated code release: the data science team changes a model, the rules team updates code that reads a model-specific field, a release train carries both, and the decision that ran yesterday can no longer be reproduced exactly.

RTDP is designed so that routine model, ruleset, threshold and product-binding changes ship as validated configuration, not code. Three further goals follow from that:

- Reproducibility. Every decision is pinned to one exact, immutable bundle of configuration, so any past decision can be explained and replayed.

- Isolation. Many tenants share product definitions but never share transactions, features, caches or actions.

- Accounted-for effects. A decision, the intent to act on it, and the confirmed external effect are recorded separately, so a timeout never leads to a blind duplicate action.

## 3. Design principles at a glance

- The control plane compiles; the decision plane executes. Configuration is validated and frozen before any transaction uses it. The control plane never processes a transaction.

- Models publish signals; rules express policy. The coupling point between data science and business policy is a versioned signal contract, not a model class, a private table or a coordinated release.

- One pinned bundle per decision. Each request selects one activation manifest at the start and carries exact versions through every call. There is no mutable "latest" on the request path.

- Tenant isolation is end to end. Tenant, environment and mode are part of every key, cache, topic, audit record, replay and idempotency key.

- Decision ≠ action intent ≠ acknowledged effect. Three separate records, three separate guarantees.

- Decision latency and feature freshness are separate targets. The synchronous path reads a served snapshot; streaming aggregation improves future reads without blocking the current one.

- Configuration-only has a boundary. Some changes are safe as configuration; new runtimes, operators, integrations or semantics are engineering changes. The platform does not pretend otherwise.

- Evidence over claims. "No code change was needed" is proven with image digests, migration versions and rule-source hashes, not asserted.

## 4. Glossary

| Term | Meaning in RTDP |
|---|---|
| Tenant | A client organization with its own subscriptions, overlays, data and actions. |
| Environment / mode | Environment is dev, test or prod. Mode is live, shadow or replay. Both are part of every identity. |
| ProductDefinition | A platform-level decision product, e.g. claim_decisioning@1. Shared definition, never shared data. |
| Overlay | A tenant-specific, allowlisted adjustment to a product (for example a different decline threshold). |
| Signal | A typed, meaningful value a model or provider publishes, e.g. a fraud probability, with freshness and provenance. |
| Signal contract | The versioned definition of a signal: value schema plus semantic meaning (units, label, horizon, population). |
| Envelope | The transport wrapper around any signal: identity, timestamps, status. |
| Provider binding | The approved link between a signal contract and the model or provider that produces it, with a deadline. |
| Bundle | An immutable snapshot of everything a decision needs: product, overlay, rules, signal contracts, bindings. |
| Dependency closure | The complete, exact set of versions a bundle depends on. Nothing floats. |
| Digest | A cryptographic hash of the bundle. Approval binds to the digest; activation rejects any drift. |
| Activation manifest / epoch | The pointer from tenant + environment + cohort to a bundle digest. Each change creates a new epoch. |
| Cohort | A stable subset of tenants or entities used for canary rollout. |
| Tier 1 / Tier 2 features | Tier 1: per-entity state updated atomically on the request path. Tier 2: aggregates computed by Flink and read as versioned snapshots. |
| Contribution | The canonical event a decision emits so streaming aggregates can be updated. |
| Decision fact | The immutable record of an outcome, its inputs, the pinned policy and the reasons. |
| Action intent | A permitted request to do something external (for example place a hold), with a target, TTL and stable key. |
| Action ledger | The durable record of every intent and its dispatch state, used for dedup and reconciliation. |
| CEL | Common Expression Language, used for declarative rule predicates. |

## 5. View 1: Configurable decisioning architecture

Figure 1. Control plane, decision plane and async paths

The idea in one line: configuration is compiled and frozen in one place, transactions are executed in another, and slow or external work happens off the critical path.

### 5.1 Three planes

The diagram is split into three horizontal bands. The control plane (purple) turns authored configuration into approved, immutable bundles and activation manifests. The decision plane (blue) executes one transaction at a time against exactly one pinned bundle. The async paths (green) handle everything that must not slow a decision: streaming aggregation, external actions, and the audit journal.

The only link from control plane to decision plane is the dashed arrow from the activation manifest to product selection. The decision plane reads which bundle is active; it never reads draft configuration and never writes to the control plane.

### 5.2 Control plane: compile configuration, never execute transactions

| Stage | What it does | Why it matters |
|---|---|---|
| Catalog + tenants | Records tenants, ownership, product subscriptions and the overlays each tenant is allowed. | Who may change what is data, not tribal knowledge. |
| Versioned registries | Stores signals, features, models, rules, flows and actions, each with immutable versions. | Every artifact can be referenced by exact version. |
| Validation + approval | Checks types, semantic compatibility and policy; runs simulation and isolation tests; then a human approves. | Breaking changes are caught before activation, not in production. |
| Immutable bundle | Resolves the exact dependency closure, hashes it into a digest and signs it. | What was approved is byte-for-byte what executes. |
| Activation manifest | Maps tenant / environment / cohort to a bundle digest and increments the epoch. | Activation and rollback become pointer moves, not deployments. |

### 5.3 Decision plane: one pinned bundle per execution

| Step | What it does | Design notes |
|---|---|---|
| Trusted ingress | Authenticates the caller, binds the tenant, tokenizes sensitive fields, assigns a correlation ID. | Tenant comes from the credential, never from the payload. |
| Product selection | Allows only products the tenant is entitled to and pins the effective configuration at request start. | The pin (manifest epoch → digest) travels with the request. |
| Features + signals | Reads Tier 1 and Tier 2 features and calls signal providers; checks each result against its provider contract. | Type, freshness and provenance are validated, not assumed. |
| Rules + aggregation | Evaluates declarative policy over the signals and aggregates the outcome. | A missing signal triggers an explicit, declared fallback, never a silent default. |
| Durable publication | Writes the decision fact and an audit snapshot of inputs; emits feature contributions and action intents. | Downstream work starts only from durable records. |

Two notes on the diagram matter. First, the signal provider in the MVP is a real ONNX model served over gRPC; pre-scored and Kafka-based provider adapters are proposed extensions, not MVP scope. Second, loose release coupling does not remove execution dependencies: if a rule needs a signal, the decision still waits for it, but only up to a bounded deadline, after which the declared fallback applies.

### 5.4 Async paths

| Path | What it does | Why it is asynchronous |
|---|---|---|
| Flink + materializer | Turns canonical contributions into event-time tiles and applies transactional updates to the Tier 2 store. | Aggregation over windows should never add latency to a decision. |
| Action Dispatcher | Executes permitted intents through authorized adapters with an idempotency ledger; records ACK, FAILED or UNKNOWN and reconciles. | External systems are slow and unreliable; their failures must not corrupt the decision. |
| Journal + read models | Keeps immutable facts and captured inputs for trace, search, replay and governed feedback. | Audit and analytics read from the journal, not from the hot path. |

## 6. View 2: Independent evolution through signal contracts

Figure 2. Models publish signals; rules express policy

The idea in one line: the stable contract is the coupling point, so a model can be retrained or replaced without touching rules, and rules can change without touching models.

### 6.1 The chain from model to action

| Link | Contents | Who changes it |
|---|---|---|
| Model artifact | Weights, preprocessing, version and an immutable digest. | Data science |
| Provider binding | The approved provider/model for a signal, the contract it honors and its deadline. | Platform, with approval |
| Canonical signal | Tenant, context and typed values, with meaning, freshness and provenance. | Contract owner (versioned) |
| Declarative rules | Named signal dependencies, thresholds and typed outcomes. | Business policy owners |
| Action policy | Which intents are allowed, through which adapter, under whose authority, until when. | Operations / risk, with approval |

Rules reference a named signal at a contract version, for example a claim fraud score defined as a calibrated probability between 0 and 1 for auto physical-damage claims at first notice of loss. Rules never reference a model class, a feature table private to the model, or a model version. That is what lets the two sides move independently.

### 6.2 Three distinct version axes

| Axis | What it versions | How often | What a change implies |
|---|---|---|---|
| Envelope version | Transport identity, time and status fields. | Rarely | May need client code changes. |
| Signal contract version | The value schema AND its semantic meaning: units, label, horizon, population. | Occasionally | Rules must opt in to the new meaning. |
| Model artifact version | Weights and supported implementation. | Often | Can change with rule source untouched, as long as the contract holds. |
| Why "semantic meaning" is part of the contract Two models can emit the same schema, a float between 0 and 1, and mean different things. If a model is retrained on a different fraud label, a different time horizon or a different population, a 0.8 no longer means what the rules were tuned for. So a change in meaning is a new contract version even when the schema is identical. Schema-compatible is not the same as semantically compatible. |

### 6.3 What is configuration-only, and what is not

| Configuration-only (inside supported capabilities) | Not configuration-only (may need code or a migration) |
|---|---|
| Threshold or predicate changes using existing inputs | A new provider or model runtime |
| A compatible model artifact with its binding | An unsupported feature or rule operator |
| An optional registered signal value | A new action integration or new authority |
| A pinned tenant product overlay | A new query/index or transport envelope |

Configuration-only changes follow validate → approve → preload → activate. The right-hand column is stated plainly on the diagram because it prevents a common failure: promising "zero code" and then smuggling engineering changes in as configuration.

### 6.4 The proof test

The required test is concrete: a threshold or model change alters the expected decision behavior while the service image digests and the database migration version stay unchanged. If the images or migrations changed, it was not a configuration-only change, whatever anyone says.

## 7. View 3: Tenant-aware catalog and product selection

Figure 3. Multi-product catalog; isolated tenant behavior

The idea in one line: tenants share product definitions, never data, and each tenant's behavior comes from its own pinned configuration.

### 7.1 Shared definitions, separate effective configurations

The platform publishes product definitions such as claim_decisioning@1, underwriting_decisioning@1 and risk_pricing@1. Tenants subscribe explicitly, pin a base version, and may apply only allowlisted overlays. Sharing a definition does not share transaction or feature data, and there is no automatic cross-tenant access.

### 7.2 Worked example: same score, different outcome

|  | Tenant A | Tenant B |
|---|---|---|
| Base product version | claim_decisioning rev 4 | claim_decisioning rev 2 |
| Other subscriptions | Underwriting + pricing | Underwriting + pricing |
| Review threshold | ≥ 0.70 | ≥ 0.70 |
| Decline threshold | ≥ 0.90 | ≥ 0.85 (tenant overlay) |
| Model probability for the claim | 0.87 | 0.87 |
| Outcome | REVIEW | DECLINE |

The example deliberately holds the score constant to isolate the effect of policy: 0.87 is above both review thresholds, below Tenant A's decline threshold of 0.90, and above Tenant B's overlay of 0.85. In real use, tenants also have different feature histories, so their scores would differ too.

### 7.3 Request flow for a tenant

| Step | What happens |
|---|---|
| Authenticate | Bind the trusted tenant from the credential; reject unknown identities. |
| Select | Confirm an active subscription and that the event type and region are eligible. |
| Pin compiled config | Resolve the overlay into a digest and record the manifest epoch. |
| Execute | Use only this tenant's feature and signal state; aggregate explicitly. |
| Authorize action | Apply the tenant's action policy and adapter; no implicit privileges. |

### 7.4 Isolation is an end-to-end invariant

- Tenant + environment + mode are part of keys, caches, topics and correlation IDs, audit, replay, and action idempotency.

- Catalog visibility is not execution entitlement: seeing a product does not mean you may run it.

- A parent identity does not get implicit access to child-tenant data.

| Still needs business confirmation The real tenant hierarchy, entitlements, override limits, conflict policy, data residency and decision authority. The diagram shows a proposed default, not a settled business model. |
|---|

## 8. View 4: Real Flink and AI inference in the MVP

Figure 4. Decision latency and feature freshness are separate targets

The idea in one line: a decision reads a consistent snapshot quickly; streaming aggregation updates that snapshot for future decisions without holding up the current one.

### 8.1 The synchronous path (top row)

| Component | Responsibility | Detail |
|---|---|---|
| Orchestrator | Pins the configuration and the deadline; deduplicates the transaction. | A repeated request with the same transaction ID returns the original decision. |
| Feature Service | Tier 1 atomic update and Tier 2 versioned read. | Tier 1 holds per-claimant and per-currency state; the current event is counted exactly once with atomic dedup; includes a vector cache. |
| ONNX inference | Serves the exact, warm model artifact and returns a canonical signal envelope. | The model fixture is a real artifact stored in MinIO with its schema, warmed up and parity-checked. No random score stubs. |
| Rules | Uses a valid signal or the declared fallback; captures every decision input. | Captured inputs make replay exact. |
| Kafka commit | Publishes egress, audit and action intents, plus one canonical contribution. | One transactional commit, so downstream consumers see all or nothing. |

### 8.2 The feature freshness loop (bottom row)

| Component | Responsibility | Why it is built this way |
|---|---|---|
| Contributions | One source per aggregate; a tokenized canonical event. | If two streams fed the same aggregate, counts would double. |
| Flink 2.x | Event-time windows, watermarks and late-data handling. | Aggregates reflect when things happened, not when they arrived. |
| feature.updates | A transactional Kafka sink read by a read_committed consumer. | Consumers never see uncommitted or aborted results. |
| Materializer | Writes absolute values with source offsets; replay-safe. | Writing "the value is 7 as of offset N" instead of "add 1" means replays cannot double count. |
| Tier 2 store | Tenant provider tiles with coverage and freshness metadata. | The decision knows how fresh its inputs are and can apply policy when they are stale. |
| The key semantic Synchronous model scoring uses the served snapshot. Flink updates affect future reads; they do not guarantee that the current event is included in its own aggregates. Where the current event must count, it goes through Tier 1, which is updated atomically on the request path. |

### 8.3 Proposed targets

| Target | Proposed value | Meaning |
|---|---|---|
| Decision latency | p99 &lt; 100 ms | End-to-end synchronous decision. |
| Feature visibility | p95 ≤ 5 s | Time for a contribution to be visible in Tier 2. |
| Staleness policy | After 30 s | If the pipeline is stale beyond this, the declared policy applies (for example route to review). |

These are proposals to be demonstrated, not claims. On delivery guarantees the design is deliberately modest: checkpoints recover managed state, Kafka transactions and idempotent stores define narrower guarantees, and there is no global exactly-once claim.

### 8.4 What the MVP must validate

The default Docker Compose environment must start real Flink and real inference, and the test suite must cover:

- Duplicates: the same event delivered twice changes aggregates once.

- Lateness: late events land in the right window or follow the declared late-data rule.

- Idle partitions: watermarks still advance when a partition goes quiet.

- Currency keys: amounts in different currencies never aggregate together.

- Crash recovery: killing Flink or the materializer mid-stream recovers without loss or double counting.

## 9. View 5: Authorized actions and acknowledged effects

Figure 5. Decision ≠ action intent ≠ acknowledged effect

The idea in one line: deciding to do something, being permitted to do it, and confirming it actually happened are three different facts, and the platform records each separately.

### 9.1 From decision to confirmed outcome

| Stage | Contents | Purpose |
|---|---|---|
| Decision fact | Immutable outcome, pinned policy and reasons. | What was decided and why. |
| Permitted intent | Tenant action policy applied; target, TTL and a stable key. | Only allowed actions become intents, and they expire. |
| Action ledger | Inbox dedup with payload hash; lease with fencing generation. | One owner at a time; stale workers are fenced out. |
| Approved adapter | Sends the idempotency key to the provider; bounded deadline and retry. | The provider can dedupe too. |
| Observed outcome | ACK, FAILED or UNKNOWN, with the provider's reference. | What actually happened, as far as we can prove. |

### 9.2 Dispatch state machine

| State | Meaning | Can move to |
|---|---|---|
| PENDING | Not yet sent. | DISPATCHING, or expired/cancelled. |
| DISPATCHING | The request may have reached the provider. | ACKNOWLEDGED or FAILED if the answer is definitive; UNKNOWN on timeout. |
| UNKNOWN | A timeout is not proof of failure. | RECONCILING. |
| RECONCILING | Query the provider before any retry. | ACKNOWLEDGED, FAILED or MANUAL_REVIEW. |
| ACKNOWLEDGED / FAILED / MANUAL_REVIEW | Confirmed provider outcome, or explicit manual investigation. | Terminal. |

### 9.3 Stable action identity

idempotency_key = tenant + environment + decision + generation + action + target. The retry attempt number is deliberately not part of the key, so every retry of the same action carries the same identity. If a different payload arrives under an existing key, it is rejected as a conflict rather than silently overwriting.

### 9.4 Worked failure scenario

A decision produces an intent to place a hold with an external provider. The adapter sends the request and the call times out after its deadline.

- A naive system retries, and if the first call actually succeeded, the hold is placed twice.

- RTDP moves the action to UNKNOWN, not FAILED, because a timeout proves nothing.

- Reconciliation queries the provider using the idempotency key. If the hold exists, the action becomes ACKNOWLEDGED with the provider reference.

- If the provider confirms it never received the request, a retry with the same key is safe.

- If the provider cannot answer, the action goes to MANUAL_REVIEW. The system never guesses.

### 9.5 Safety invariants

- Replay and shadow modes are prohibited from live dispatch at both the orchestration and adapter boundaries.

- A provider timeout after a possible effect goes to UNKNOWN and then reconciliation, never a blind duplicate call.

- A lease alone cannot prevent duplicate external effects; provider-side idempotency and fencing still matter.

- Rollback changes future policy. It does not undo an acknowledged effect.

In the MVP all adapters are local simulators. Real enforcement authority and provider guarantees must be explicitly confirmed before any real integration.

## 10. View 6: Validated configuration and independent activation

Figure 6. Deploy artifacts independently; activate compatible configurations

The idea in one line: a bundle is a consistent configuration snapshot, not a reason to rebuild every service.

### 10.1 Lifecycle of a configuration change

| State | Gate | What is checked |
|---|---|---|
| DRAFT | Immutable new version | An owner and tenant scope are recorded; drafts are never edited in place. |
| VALIDATED | Dependency closure | Types, semantics and replay against captured decisions. |
| APPROVED | Role separation | A different person approves; approval binds the bundle digest. |
| PREPARED | Download + verify + warm | Artifacts downloaded and verified by digest; features and models confirmed ready. |
| ACTIVE | Tenant / env / cohort | A new manifest epoch points the chosen scope at the bundle. |

### 10.2 Immutable dependency closure

- The bundle contains the exact product, overlay, rule and signal contract versions, nothing floating.

- Approval binds the bundle digest, and approvers cannot edit content.

- Model-service identities hold no approval rights (ADR-011). A model can publish signals or draft suggestions; it can never approve, activate or dispatch.

- If the digest at activation differs from the approved digest, activation is rejected.

### 10.3 Request-pinned execution

- Select one manifest at request start and pass exact versions in every call.

- No mutable "latest" on the request path.

- One bundle digest per decision, and the selection reasons are recorded on the decision fact.

This is what makes a mid-flight activation safe: a request that started on epoch 41 finishes on epoch 41 even if epoch 42 activates halfway through.

### 10.4 Rollout ladder

| Step | What happens | Safeguard |
|---|---|---|
| Shadow | The new bundle runs on real traffic with isolated state. | No live effects. |
| Canary | A stable tenant or entity cohort moves to the new bundle. | Decision and action differences are compared. |
| Promote | Wider activation after gates pass. | Quality and latency gates plus business approval. |
| Rollback | A new epoch points back to the previous bundle. | History is preserved; nothing is overwritten. |
| Emergency stop | Blocks new dispatch for a scope. | The actor and reason are audited. |

Configuration-only changes still require validation and authorization. New runtimes, adapters or signal semantics are engineering changes, not overlays.

## 11. View 7: Delivery plan and proof

Figure 7. Build the architecture, then prove independent evolution

The idea in one line: build the real architecture first, then prove that it evolves independently, with evidence at every gate.

| Phase | Scope | Exit evidence |
|---|---|---|
| 0 · Contracts | Envelope and semantic schemas; tenant fixture with pinned dependencies; Compose harness. | Schemas versioned; harness starts cleanly. |
| 1 · Real MVP | Two tenants; real Flink and ONNX; signals, CEL rules, bundles, action ledger; multi-product: claims, underwriting, pricing. | Threshold and model changes shown without rebuilds; failure tests pass. |
| 2 · Self-service | Catalog, overlay and rule UI; validation and approvals; prepare / canary / promote. | A non-engineer ships a threshold change through the UI with approval. |
| 3 · Extend | External signal adapters if needed; bounded correlation and late scores; new product domains via config. | A new product domain added without platform code changes. |
| 4 · Feedback | Point-in-time datasets and replay; challenger evaluation; recommendations become proposals. | Challenger results feed proposals that still go through approval. |
| 5 · Harden | Real integrations only with authority; residency, recovery and isolation drills; provisional authority grant. | Drills passed and documented. |
| Delivery rules No stubs, and no silently weakened acceptance tests. MVP gate: threshold and model changes take effect without rebuilds, with per-gate evidence in docs/validation. Image digests, migration history and rule-source hashes are the evidence that a change was configuration-only. |

## 12. End-to-end walkthrough: one synthetic claim

A claim event arrives for Tenant B. This follows it through every view.

- Ingress. The request authenticates; Tenant B is bound from the credential; the claimant ID is tokenized; a correlation ID is assigned. (View 1)

- Selection and pinning. Tenant B is subscribed to claim_decisioning. The orchestrator reads the manifest, finds epoch 42 pointing at digest 9f3c…, and pins it with a 100 ms deadline. (Views 3 and 6)

- Features. Tier 1 atomically records this claim for the claimant, counted once. Tier 2 returns a versioned snapshot of 30-day claim counts, marked fresh as of 3 seconds ago. (View 4)

- Signal. The ONNX provider bound to the fraud-score contract returns 0.87 in a canonical envelope. The contract check passes: right type, fresh, correct provenance. (Views 2 and 4)

- Rules. Tenant B's overlay sets decline at ≥ 0.85, so the outcome is DECLINE; all inputs are captured. (View 3)

- Publication. One Kafka transaction commits the decision fact, the audit snapshot, a canonical contribution and an action intent to notify the claims system. (Views 1 and 4)

- Aggregation. Flink picks up the contribution, updates the event-time window, and the materializer writes the new absolute value with its offset to Tier 2. The next decision for this claimant sees it within seconds. (View 4)

- Action. The dispatcher checks Tenant B's action policy, records the intent in the ledger with its idempotency key, and calls the simulator adapter. It acknowledges; the outcome is ACKNOWLEDGED with a provider reference. (View 5)

- Audit and replay. A month later someone asks why this claim was declined. The journal shows the pinned digest, every input, the 0.87 score and the 0.85 threshold. Replay in shadow mode reproduces DECLINE exactly, and no live action fires. (Views 1 and 5)

- Change. Tenant B later raises its decline threshold to 0.90. That is a configuration-only change: validated, approved, canaried and activated as epoch 43, with no image or migration changes. New claims at 0.87 now go to REVIEW; the old decision stays DECLINE on its original epoch. (Views 2 and 6)

## 13. Architecture decision records

| ADR | Decision |
|---|---|
| ADR-001 | Models and rules communicate through versioned semantic signals. |
| ADR-002 | Stable envelope plus validated extensible payloads; no per-model tables. |
| ADR-003 | Tenant-qualified catalog, state, correlation and action identities. |
| ADR-004 | Immutable effective bundles and request-pinned activation epochs. |
| ADR-005 | Synchronous Tier 1 with asynchronous Flink Tier 2. |
| ADR-006 | Inference transport behind a provider interface, gRPC first. |
| ADR-007 | Decision facts, action intent and acknowledged effect are separate. |
| ADR-008 | Explicit boundaries for Kafka transactions, store dedup and provider idempotency. |
| ADR-009 | Configuration-only changes have a supported-capability boundary. |
| ADR-010 | Replay and shadow cannot perform live effects. |
| ADR-011 | Language models act only as contracted signal providers or advisory, draft-only assistants; they never decide outcomes, transition state or dispatch actions. |
| ADR-012 | Multi-step state is modeled as declarative, versioned state-flow assets executed by a deterministic, pinned flow engine. |

## 14. Open questions and risks

| Area | Open question or risk | Where it shows up |
|---|---|---|
| Tenant model | Real hierarchy, entitlements, override limits and conflict policy need business confirmation. | View 3 |
| Residency | Where each tenant's data and processing may live. | Views 3, 7 (Phase 5) |
| Authority | Who may authorize real external actions, and under what grant. | Views 5, 7 |
| Provider guarantees | Whether real providers support idempotency keys and status queries for reconciliation. | View 5 |
| Latency targets | p99 &lt; 100 ms and p95 ≤ 5 s freshness are proposals until measured. | View 4 |
| Scope creep | Pressure to label engineering changes as configuration. | Views 2, 6 |
| Exactly-once expectations | Stakeholders may assume global exactly-once; the design guarantees narrower, explicit boundaries. | View 4 |

## 15. Explaining RTDP in conversation

### 15.1 The 60-second version

"RTDP separates three things most decision platforms tangle together. Models publish versioned signals and rules express policy, so data science and the business can ship independently. Every decision runs on one immutable, approved bundle, so it can be explained and replayed exactly. And a decision, the intent to act on it, and the confirmed external effect are recorded separately, so a timeout never becomes a duplicate payment or hold. Routine threshold and model changes are configuration, and the proof is that image digests and migrations don't change."

### 15.2 Questions an architecture review board will ask

| Question | Short answer |
|---|---|
| How do you prevent a model change from breaking rules? | Rules depend on a signal contract, not a model. Meaning changes create a new contract version that rules must opt into; validation replays captured decisions before approval. |
| How do you roll back? | Activate a new epoch pointing at the previous bundle. It is a pointer move; history is preserved; in-flight requests finish on their pinned epoch. |
| What happens if a downstream call times out? | The action goes to UNKNOWN, then reconciliation queries the provider with the same idempotency key. No blind retry; unresolved cases go to manual review. |
| Do you guarantee exactly-once? | No global claim. Kafka transactions, idempotent stores and provider idempotency each give explicit, narrower guarantees, and the tests prove them. |
| Where can an LLM participate? | Only as a contracted signal provider or a draft-only assistant (ADR-011). It never approves, decides outcomes, transitions state or dispatches actions. |
| How is tenant isolation enforced? | Tenant, environment and mode are in every key, cache, topic, audit record, replay and idempotency key; entitlement is checked at selection and at action authorization. |