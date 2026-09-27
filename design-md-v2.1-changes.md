# design.md v2.1 changes

Apply these edits to `docs/design.md` v2.0. Everything not listed is unchanged. The same edits are already applied in `RTDPdesign_v2.1.docx`.

## 1. Title block

Replace the version line with:

```text
Detailed system design • Version 2.1 • September 27, 2026 • Implementation target: Devin
```

Add this paragraph directly below it:

> Version 2.1 adds declarative state flows and bounded language-model roles (ADR-011, ADR-012). All other v2.0 content is unchanged.

## 2. Reading guide table

Add after the "Real-time action execution" row:

```text
| Declarative state flows and model-assisted authoring | How do multi-step states evolve, and where may language models participate? |
```

## 3. Service responsibilities table

Add after the "Action Dispatcher" row:

```text
| Flow Engine | Pinned state-flow instances, guard evaluation, durable timers, transition facts, emitted action commands | Live model calls to choose transitions, or direct provider side effects |
```

## 4. New section

Insert after "Reassessment and kill switches" (end of "Real-time action execution") and before "## Persistence and data ownership":

````markdown
## Declarative state flows and model-assisted authoring

Some products need multi-step state beyond a single decision: review-case lifecycle, step-up authentication, hold-and-release, and reassessment. Model these as declarative state-flow assets compiled into the runtime bundle, the same way rulesets are. Do not implement them as product-specific code branches, and do not let a live model call choose the next state.

A language model may help author, test, and operate these flows. It may not execute them. The flow engine that applies transitions is deterministic, pinned to a bundle digest, and replayable.

### State-flow asset

A state flow declares states, transitions, guards, timers, and the registered action types each transition may emit. Guards are CEL expressions evaluated under the same sandbox and cost limits as rules. The example below is synthetic.

```yaml
flow_id: card_review_case
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
action_policy: card_case_actions@1
```

`emits` names registered action types only. The compiler rejects any action not permitted by the pinned action policy, and emitted commands follow the durable dispatcher protocol unchanged.

### Flow engine runtime rules

1. Key every flow instance by tenant, environment, mode, flow id, and subject identity. A new instance pins the flow version and bundle digest at creation.
2. Consume a transition event through the inbox, deduplicating by tenant and event id. The same id with a different payload is a conflict.
3. Select candidate transitions whose `from` includes the current state and whose `on` matches the event type and contract version.
4. Evaluate guards against canonical inputs. A guard whose required signal is missing, invalid, or expired never evaluates to false by default; apply its declared `on_missing_signal` behavior (`HOLD` or a named fallback transition).
5. Apply exactly one transition. When several share a source state and event, the compiler requires distinct explicit priorities; if more than one guard still matches at equal priority, reject with `FLOW_CONFLICT` and record it. Never use declaration or iteration order as policy.
6. In one Postgres transaction, update the instance with optimistic concurrency on its state version, append the transition fact, and write emitted action commands to the outbox.
7. Schedule declared timers durably. A timer firing is an event with a stable id, so a restart cannot fire it twice.

In-flight instances continue on their pinned flow version when a new version is activated. Moving live instances to a new version requires an approved migration asset that maps old states to new ones; it is never implicit. Replay and shadow instances run in isolated namespaces and cannot emit live actions.

### Flow compilation checks

- **Reachability:** every state is reachable from `initial`, and every non-terminal state has at least one exit through an event or timer.
- **Terminal states:** no transition may leave a terminal state.
- **No automatic loops:** any cycle must include an external event or a timer; guard-only cycles are rejected.
- **Types:** guards type-check against registered event, signal, actor, and configuration schemas.
- **Authority:** emitted actions are permitted by the pinned action policy, and transitions requiring human roles declare them.
- **Budgets:** guard expression size and cost stay within limits, and every non-terminal state declares an SLA timer unless an approved exemption exists.

### Where language models participate

Language models, large or small, operate around the decision path, not inside its authority. The governing pattern: a model may produce contracted signals or propose drafts. Only the compiled, pinned bundle decides and transitions state, and only the dispatcher acts under action policy.

| Role | Placement | Authority | Boundary |
|---|---|---|---|
| Signal provider | Classify merchant descriptors, free-text fields, and case notes into registered signals such as `merchant.category_risk` or `case_triage.priority` | Produces evidence only | Behind `SignalProvider` with a semantic contract, binding, fallback, and shadow evaluation; precomputed or asynchronous by default |
| Case assistant | Summarize a review case and suggest a disposition | Advisory | Runs after the case action is acknowledged; a human submits the disposition event |
| Explanation writer | Plain-language narrative from fired rules and the signal snapshot | Advisory | Asynchronous from `decision_fact`; code-generated reason codes remain authoritative |
| Authoring assistant | Draft rules, overlays, thresholds, and state flows from feedback, replay diffs, and incident notes | Draft only | Enters as a DRAFT asset; normal compile, simulation, replay diff, and separate human approval |
| Operations triage | Cluster UNKNOWN actions and stuck flow instances; suggest reconciliation steps | Advisory | Reconciliation logic stays deterministic; suggestions require an authorized operator |

### Controls for model participation

- **Contracts first:** any model output consumed by a rule or guard is a registered signal contract with meaning, unit, allowed statuses, and a declared fallback, exactly like model scores.
- **Draft provenance:** model-assisted assets carry `origin: model_assisted` plus model id, version, prompt-template digest, and input-reference digests. Approval binds the immutable content, not the prompt.
- **No approval by a model:** model service identities cannot hold approver, activation, or action-administrator roles. Separation of duties applies to the human who submits a model-assisted draft.
- **No write path to authority:** model service identities have no write access to activations, flow instances, action commands, or the action ledger. Enforce with RBAC and database roles, not convention.
- **Untrusted input:** transaction and case text is data, never instruction. Tokenize or redact sensitive fields before a model sees them, validate outputs against a schema, and keep prompt-injection fixtures in the test suite.
- **Reproducibility:** journal the model identity, prompt-template digest, parameters, and output. Decisions and transitions never depend on re-running a model during replay.
- **Latency:** language-model calls are excluded from the 100 ms synchronous path by default. A small model may be bound as a synchronous signal provider only after it meets the inference budget at p99 under the load test, with a declared fallback.

### Delivery placement and acceptance tests

Build the flow engine in Phase 2, with the review-case flow as its first asset. Model-backed signal providers follow the Phase 3 transport work. Case assistant, explanation, authoring, and triage assistants belong in Phase 4.

| Gate | Pass condition |
|---|---|
| Flow compilation | Unreachable state, exit from a terminal state, guard-only cycle, unpermitted action, and missing SLA timer are each rejected with a structured diagnostic |
| Pinned flow version | Activating flow v2 leaves in-flight v1 instances on v1; only an approved migration moves them |
| Transition idempotency | A duplicate event produces one transition and one set of action commands |
| Guard ambiguity | Two matching guards at equal priority produce `FLOW_CONFLICT` and no transition |
| Missing guard signal | A missing or expired signal produces the declared HOLD or fallback, never an implicit false |
| Durable timers | An SLA timer fires exactly once across a scheduler restart |
| Model authority | A model service identity cannot write activations, flow instances, action commands, or the action ledger |
| Model-assisted draft | A model-assisted asset cannot activate without separate human approval, and its provenance is recorded |
| Prompt injection | Malicious merchant descriptors and case notes do not change signal schemas, flow transitions, or permitted actions |
| Replay isolation | Flows replayed or shadowed emit zero live actions |
````

## 5. Logical tables

Add after the `action_execution` row:

```text
| flow_instance | tenant_id, environment, mode, flow_id, subject_id | pinned flow version and bundle digest; state version for optimistic concurrency |
| flow_transition | tenant_id, mode, instance key, transition_seq | append-only transition facts with event id and guard inputs |
```

## 6. Architecture decisions to record

Add after ADR-010:

```text
| ADR-011 | Language models act only as contracted signal providers or advisory, draft-only assistants; they never decide outcomes, transition state, or dispatch actions |
| ADR-012 | Multi-step state is modeled as declarative, versioned state-flow assets executed by a deterministic, pinned flow engine |
```

## 7. Devin starter prompt

Change "version 2.0" to "version 2.1", and append this sentence at the end of the prompt:

> Declarative state flows are Phase 2 and language-model assistants are Phase 4; in Phases 0 and 1, grant no model service identity write, activation, or dispatch authority (ADR-011).
