# RTDP v2.2 — Agent Control Plane

Extends RTDP v2.1 (decision ≠ intent ≠ effect; bundles; action policy as data; Kafka commit boundary; tenant isolation; ADR-001..012) so that **agents**, not only services, can be callers, and so that RTDP governs **actions taken by agents on behalf of people and organisations**. Eight ADRs (013–020), each closing one gap, plus two demo products that exercise them: an agentic hotel stay and an agentic benefits change.

Everything is synthetic. No real guests, participants, hotels, employers, suppliers or payment rails. No public URL. Existing Phase-1 gates G1–G7 and the benefits/receipts suites must keep passing.

## 0. What changes, in one table

| Gap (v2.1) | ADR | New / changed | Gate |
|---|---|---|---|
| Callers are services and tenants; no notion of an agent acting for a principal | 013 Agent identity & delegation | `actor` becomes a delegation chain; `agent_registry`; agent-scoped credentials and revocation | G8 |
| Action policy maps outcomes → actions, but not *who may complete what alone* | 014 Autonomy tiers & approval | `PENDING_APPROVAL` state; autonomy levels per action; accountable owner on every policy version | G9 |
| Effects go out through adapters; nothing comes *in* from third-party agents through a governed door | 015 Agent gateway (MCP door) | `agent-gateway` service exposing tools over MCP + REST; credential brokering; per-agent quotas | G10 |
| Only actions are governed; reads are not | 016 Read-side policy | Purpose-bound `read_grant`s; data classes; context isolation per task/tenant | G11 |
| One caller, one decision; no arbitration between agents | 017 Arbitration & PROPOSE | `PROPOSE` outcome with constraints; resource claims; precedence policy | G12 |
| `UNKNOWN → RECONCILE` exists; no undo for applied effects | 018 Compensation catalogue | `compensating_action` per action type; `COMPENSATING` state; retention policy for agent context | G13 |
| Ledger covers RTDP decisions only | 019 Agent registry & call graph | Every gateway call becomes an `agent_call` fact; registry of agents, owners, scopes; call-graph projection | G14 |
| Cost meter only | 020 Metering & outcome metrics | Per-agent / per-principal metering; `work_completed_by_agent` and `done_right` metrics in the ledger | G15 |

Repository placement: new services under `services/agent-gateway`, `services/approval-service`, `services/agent-registry`; schema under `internal/store/migrations`; contracts under `contracts/agent/`; assets under `assets/seed/agent/`; ADRs under `docs/adr/013..020`. Changes inside `orchestrator`, `action-dispatcher`, `rules-service` and `control-plane` are allowed **only** where this document names them.

---

## ADR-013 — Agent identity and delegation

**Context.** In v2.1 the `actor` is the authenticated client identity bound to a tenant. In an agentic world the caller is an agent (guest's agent, hotel dot, procurement dot) acting *for* a principal, possibly through sub-agents. Accountability, revocation and audit need the whole chain, not the last hop.

**Decision.** `actor` becomes a **delegation chain**. Every request carries `principal → agent → (sub-agent)*`, each link signed, each agent registered, each agent holding scoped credentials that can be revoked independently of the tenant.

**Design.**

Envelope (`internal/envelope`, proto `rtdp.Actor` → `rtdp.DelegationChain`):

```
delegation:
  principal:   { kind: PERSON|ORG|SERVICE, id, tenant_id }
  links:
    - { agent_id, agent_version, scopes: [...], issued_at, expires_at, grant_id }
    - { agent_id, ... }            # sub-agent, optional
  proof:        signed JWT/JWS over the chain, issuer = agent-registry
```

Rules: the chain is validated at `ingress` (signature, expiry, revocation list) and passed intact to `orchestrator`; CEL rules see `actor.principal.*`, `actor.agent.*` (last link) and `actor.chain_depth`. Max depth is a tenant setting (default 2). A link whose `scopes` do not cover the product's required scope → `DECLINE_UNAUTHORIZED` before rules run (decision fact still written).

Agent registry (`services/agent-registry`, Postgres):

```
agent(agent_id, tenant_id, kind: CUSTOMER|EMPLOYEE|VENDOR|PLATFORM,
      owner_role, owner_identity, status: ACTIVE|SUSPENDED|REVOKED,
      allowed_scopes[], max_autonomy, created_at)
agent_credential(cred_id, agent_id, kid, not_before, not_after, revoked_at)
agent_grant(grant_id, principal_id, agent_id, scopes[], purpose, not_after, revoked_at)
revocation(kid|grant_id, revoked_at, reason)   # projected to Redis set, TTL = max token life
```

Revocation is per credential **and** per grant: a person can revoke what their agent may do for them without the tenant revoking the agent.

AWS: agent credentials are short-lived tokens minted by `agent-registry` using a KMS asymmetric key; the revocation set lives in ElastiCache Valkey; registry tables in Aurora; audit of grant changes on an MSK topic `agent.grants`.

**Gate G8 — delegation.** (a) A request with a valid chain decides normally and the decision fact stores the full chain. (b) Revoke the *grant* → the same request returns `DECLINE_UNAUTHORIZED` within one revocation-propagation interval (≤ 5 s) while the agent's credential still works for a different principal. (c) Chain depth 3 on a tenant with max 2 is rejected at ingress. (d) A tampered link fails signature verification.

---

## ADR-014 — Autonomy tiers, approval state and accountable owner

**Context.** v2.1 action policy says which outcome may emit which action. It does not say whether an agent may *complete* that action alone, under what conditions a human must approve, or who owns the outcome.

**Decision.** Action policy gains **autonomy tiers**, a **`PENDING_APPROVAL`** decision state with SLA and escalation, and a mandatory **accountable owner** on every policy version. Rules may return `APPROVE_WITH_APPROVAL` which the orchestrator maps to `PENDING_APPROVAL`.

**Design.**

Action policy (`contracts/.../action_policy.yaml`):

```
owner:            { role: "Director, Guest Operations", identity: "owner@example.test" }  # required
autonomy_tiers:
  T0: { completes_alone: false }                       # always human
  T1: { completes_alone: true, conditions: ["input.amount <= cfg.t1_limit"] }
  T2: { completes_alone: true }
actions:
  ISSUE_REIMBURSEMENT:  { tier: T1, approvers: ["role:benefits_reviewer"], sla_minutes: 240, on_sla_breach: ESCALATE }
  ASSIGN_ROOM:          { tier: T2 }
  SET_ROOM_HVAC:        { tier: T2 }
  CHANGE_CONTRIBUTION:  { tier: T1, approvers: ["role:plan_admin"], sla_minutes: 1440, on_sla_breach: EXPIRE }
```

Tier resolution happens in the orchestrator **after** rules: outcome `APPROVE` + action tier T1 with conditions false, or T0 → decision state `PENDING_APPROVAL`; the action intents are written with `status: AWAITING_APPROVAL` in the same Kafka transaction (nothing dispatches). `max_autonomy` on the agent (ADR-013) caps the tier: an agent registered at T1 can never complete a T2 action alone.

Approval service (`services/approval-service`): `POST /v1/approvals/{decision_id}` with `{approver_identity, verdict: APPROVE|REJECT, note}`; verifies the approver matches `approvers`, writes `approval_event`, and publishes `action.commands` for the held intents (APPROVE) or marks them `CANCELLED` (REJECT). SLA timer: `timer.approval_due_at`; breach → `ESCALATE` (re-route to `escalation_approvers`) or `EXPIRE`. Approvals are themselves ledger facts with a human identity; the agent cannot approve its own request (checked against the delegation chain).

Decision state machine (orchestrator): `DECIDED → PENDING_APPROVAL → (APPROVED | REJECTED | EXPIRED)`; only `APPROVED` releases intents. Shadow/replay cannot create approvals (ADR-010 extended).

AWS: approval queue on MSK `approval.requests`; SLA timers via the existing timer path in Flink (or EventBridge Scheduler if Flink is not convenient); approvals UI is the existing demo UI with a new tab, served through the tunnel only.

**Gate G9 — autonomy.** (a) T2 action from a T2 agent completes with no approval. (b) The same action from an agent registered `max_autonomy: T1` lands in `PENDING_APPROVAL`. (c) Approval by a non-listed identity is rejected; by the requesting agent's own identity is rejected. (d) SLA breach escalates and the escalation is a ledger fact. (e) Every active policy version has a non-empty `owner`; compile fails without it.

---

## ADR-015 — Agent gateway: one governed door (MCP + REST)

**Context.** v2.1 only has outbound adapters. Wei's "stand up a proper connection layer" means third-party and internal agents call *in* through governed tools instead of scraping screens, and one door serves employee agents and customer agents alike.

**Decision.** A new `agent-gateway` service exposes RTDP capabilities and system-of-record operations as **MCP tools** (and REST equivalents). Every tool call is authenticated with the delegation chain, authorised against scopes and read grants, rate-limited per agent, credential-brokered to the backend, and recorded as an `agent_call` fact (ADR-019).

**Design.**

Tool surface (first set, all synthetic backends):

```
decide(event_type, input)                      → RTDP /v1/decide
reservations.lookup / reservations.create      → simulated reservation system
crm.profile.read(guest_id)                     → simulated CRM (read grant required, ADR-016)
pms.room.assign(reservation_id, constraints)   → simulated PMS (action, via RTDP decide → ASSIGN_ROOM)
bms.hvac.set(room_id, state)                   → simulated building controls (action)
housekeeping.task.add(room_id, item)           → simulated housekeeping (action)
benefits.participant.read(participant_id)      → simulated HR system (read grant)
benefits.contribution.change(participant_id, pct) → RTDP decide → CHANGE_CONTRIBUTION
approvals.list / approvals.decide              → approval-service (human identities only)
```

Rules of the door: no tool reaches a backend directly; **every mutating tool is routed through `/v1/decide`** so it inherits rules, autonomy tiers and the ledger (the gateway never has write credentials to backends, only the dispatcher does). Read tools check a `read_grant` (ADR-016). Credentials to backends are brokered per tenant from a secrets store and never returned to the agent. Per-agent quotas (`calls/min`, `concurrent`) and per-tool deadlines are gateway config. MCP server metadata advertises tools filtered by the caller's scopes, so an agent cannot see tools it cannot call.

AWS: `agent-gateway` on EKS behind the existing tunnel; backend credentials in Secrets Manager read via IRSA; quotas in Valkey; MCP over streamable HTTP.

**Gate G10 — one door.** (a) A customer agent and an employee agent call the same `reservations.lookup` tool through the same endpoint with different scopes and get correctly filtered results. (b) `pms.room.assign` produces a decision fact and an action intent; there is no code path from gateway to PMS adapter that bypasses `/v1/decide` (static check in CI). (c) Quota breach returns 429 and an `agent_call` fact with `outcome: THROTTLED`. (d) Tool list differs by scope.

---

## ADR-016 — Read-side policy: purpose binding and context isolation

**Context.** v2.1 governs actions. "The rule nobody wrote" is a read problem: valid permissions, wrong purpose, and information carried from one task into another.

**Decision.** Reads are governed by **purpose-bound read grants** over **data classes**, and agent context is **isolated per task and per tenant**. A read outside the grant's purpose is denied and recorded; data returned is tagged with its class and purpose, and the gateway refuses to pass class-tagged data into a tool call whose purpose differs.

**Design.**

Data classes on simulated systems: `PUBLIC`, `INTERNAL`, `CONFIDENTIAL_COMMERCIAL` (supplier pricing), `PII`, `PHI`.

Read grant (`contracts/agent/read_grants.yaml`, compiled into the bundle):

```
read_grant:
  id: rg.hotel.prearrival@1
  agent_kinds: [PLATFORM]
  purpose: PREPARE_STAY
  allows:
    - { tool: crm.profile.read, classes: [PII], fields: [preferences, tier] }   # not payment, not history
  denies_cross_purpose: true
read_grant:
  id: rg.procurement.renewal@1
  purpose: RENEW_CONTRACT:{supplier_id}
  allows:
    - { tool: supplier.pricing.read, classes: [CONFIDENTIAL_COMMERCIAL], scope: "supplier_id == purpose.supplier_id" }
```

Each gateway task has a `task_id` and a declared `purpose`. Responses carry `{class, purpose, task_id}` labels in metadata. The gateway maintains a per-task context manifest (what was read, under which grant); a tool call that would send labelled data into a different `purpose` or `tenant` is denied with `CROSS_PURPOSE_FLOW`. Context manifests are ledger facts; when the task ends the manifest is sealed and the working context is discarded (ADR-018 retention).

CEL gets `read.purpose`, `read.classes_seen` so rules can also condition outcomes (e.g. refuse an action if `CONFIDENTIAL_COMMERCIAL` from another supplier was read in this task).

**Gate G11 — purpose.** (a) The procurement dot reading supplier A's pricing under `RENEW_CONTRACT:A` succeeds; reading supplier B's under the same purpose is denied with a ledger fact. (b) PII read under `PREPARE_STAY` cannot be passed to a tool call tagged `MARKETING`. (c) Two tasks for two tenants on the same agent share no context (manifests disjoint; Redis keys hash-tagged by `{tenant:task}`).

---

## ADR-017 — Arbitration: PROPOSE outcome, resource claims, precedence

**Context.** v2.1 decides one request from one caller. When the guest's agent wants room 1802 and the hotel's rules reserve top-floor rooms for a different tier, or two agents claim the same room, someone has to arbitrate and the answer is often a counter-offer, not approve/decline.

**Decision.** Add a **`PROPOSE`** outcome carrying constraints (a counter-offer), **resource claims** with precedence, and a **precedence policy** between agent kinds. Agents propose; RTDP decides; conflicts resolve by policy, never by race.

**Design.**

Outcome set becomes `APPROVE | PROPOSE | REVIEW | DECLINE | PENDING_APPROVAL` (DECLINE still absent for no-reject products). `PROPOSE` carries:

```
proposal: { alternatives: [ {room: "1702", reason: "TIER_LIMIT"}, ... ], expires_at, accept_tool: "pms.room.assign" }
```

Resource claims (`internal/store` table `resource_claim(resource_key, tenant_id, holder_decision_id, precedence, expires_at)`): an action that binds a scarce resource (`room:1802`, `slot:…`) acquires a claim inside the decision transaction. Conflict → the lower-precedence request gets `PROPOSE` with alternatives computed by the rules' `alternatives` CEL list, never `DECLINE`. Precedence policy is data:

```
precedence:
  - { agent_kind: EMPLOYEE, weight: 30 }
  - { agent_kind: PLATFORM, weight: 20 }
  - { agent_kind: CUSTOMER, weight: 10 }
  tie_break: ["loyalty_tier desc", "request_time asc"]
```

Accepting a proposal is a new `/v1/decide` with `input.proposal_id`; rules verify it is unexpired and unchanged; the claim transfers atomically. A proposal is a ledger fact; expiry releases the claim.

**Gate G12 — arbitration.** (a) Two agents request `room:1802` concurrently 100 times; exactly one `APPROVE`, the other `PROPOSE`, zero double-assignments. (b) Guest agent asking for a tier-restricted room receives `PROPOSE` with alternatives, never `DECLINE`. (c) Accepting an expired proposal → `PROPOSE` again with fresh alternatives. (d) Precedence policy change (config-only) flips which agent wins; image digests unchanged (G3 pattern).

---

## ADR-018 — Compensation catalogue and retention

**Context.** `UNKNOWN → RECONCILE` handles not knowing whether an effect applied. It does not handle undoing an effect that did apply when a decision is later reversed, a proposal superseded, or a grant revoked. And disconnecting an agent does not erase what it learned.

**Decision.** Every action type declares a **compensating action** or declares itself non-compensable; a **`COMPENSATING`** state and a compensation ledger exist; and agent context has an explicit **retention policy** enforced at the gateway.

**Design.**

Action policy addition:

```
actions:
  ASSIGN_ROOM:           { compensate_with: RELEASE_ROOM,     window_minutes: 1440 }
  SET_ROOM_HVAC:         { compensate_with: RESET_ROOM_HVAC,  window_minutes: 1440 }
  ISSUE_REIMBURSEMENT:   { compensate_with: null, non_compensable_reason: "FUNDS_MOVED" }
  CHANGE_CONTRIBUTION:   { compensate_with: REVERT_CONTRIBUTION, window_minutes: 43200 }
```

Triggers: approval `REJECT` after a T2 sibling action applied; proposal superseded; grant revoked within the window; explicit `POST /v1/decisions/{id}/compensate` by an accountable owner. The dispatcher issues the compensating intent with idempotency key `…:compensate:<original_intent>`; state `ACKNOWLEDGED → COMPENSATING → COMPENSATED | COMPENSATION_FAILED → REVIEW`. Non-compensable actions route to `REVIEW` with the reason; nothing is silently left applied.

Retention: `agent.context_retention` (per agent kind, default `TASK_END`) controls how long the gateway keeps task context; sealed manifests (ADR-016) are kept for audit, working context is purged; `revoke` on a grant purges open task contexts for that principal immediately and writes a `context_purged` fact. The honest statement to the user: what was *done* is compensated where possible and recorded always; what was *read* is recorded and purged, not un-read.

**Gate G13 — compensation.** (a) Reject an approval after a T2 sibling applied → compensating intent dispatched exactly once, ledger shows both. (b) Non-compensable action → `REVIEW` fact with reason, no retry. (c) Revoke a grant mid-task → task context purged, `context_purged` fact present, sealed manifest still readable.

---

## ADR-019 — Agent registry and call graph (single pane)

**Context.** The ledger covers decisions RTDP made. The single pane Wei describes covers all agent traffic: which agents are active, calling what, on whose behalf, how often, what they completed and escalated — including agents that never reach the decision plane.

**Decision.** Every gateway call produces an **`agent_call` fact** (MSK `agent.calls`), projected into a call-graph store; the registry (ADR-013) plus the call graph is the single pane. Agents that bypass the gateway are, by construction, invisible — which is the point: the door is the pane.

**Design.**

```
agent_call(call_id, ts, tenant_id, task_id, purpose, principal_id, agent_id, agent_version,
           chain_depth, tool, backend, outcome: OK|DENIED|THROTTLED|ERROR|PROPOSED|PENDING_APPROVAL,
           decision_id?, latency_ms, cost_units, classes_read[])
```

Projections (Postgres materialised views, refreshed by the projector): `agent_activity_1h` (calls, errors, denials per agent), `call_graph_edges` (agent → tool → backend, weighted), `on_behalf_of` (principal → agents), `escalations` (PENDING_APPROVAL / REVIEW by agent). UI: a "Agents" tab in the demo UI showing active agents, the graph, and a drill-down to the decision ledger for any call.

**Gate G14 — pane.** (a) After the demo script, the graph shows every agent, tool and backend touched; counts match the ledger. (b) A denied cross-purpose read (G11) appears as a `DENIED` edge. (c) Suspending an agent in the registry makes its next call `DENIED` and the pane shows it within one refresh.

---

## ADR-020 — Metering and outcome metrics

**Context.** The cost meter prices steps. The contract conversation Wei anticipates needs a unit of value per agent and per principal, and the adoption metric she proposes is "share of work completed by agents, and done right."

**Decision.** Meter at the agent and principal level, and compute two first-class metrics in the ledger: **`work_completed_by_agent`** (decisions whose actions reached `ACKNOWLEDGED` with no human step) and **`done_right`** (no compensation, no reconciliation failure, no human override within the quality window).

**Design.**

`metering(tenant_id, period, agent_id, principal_id, tool, calls, decisions, actions_completed, approvals_needed, cost_units, est_cost)` with unit prices in `config/prices.yaml` (model calls, tool calls, human review minutes). Metrics per product and per agent kind:

```
work_completed_by_agent = completed_without_human / total_transactions
done_right              = (completed_without_human - compensated - overridden_in_window) / completed_without_human
straight_through_minutes = p50/p95 from request to ACKNOWLEDGED
```

Showback endpoint `GET /v1/metering?tenant&period&group_by=agent|principal|tool`. The eval report prints both metrics next to cost per transaction, never apart.

**Gate G15 — metrics.** (a) Demo run yields `work_completed_by_agent` and `done_right` per product with values reconcilable to ledger rows. (b) Forcing one compensation (G13) moves `done_right` by exactly one transaction. (c) Showback by principal sums to showback by agent.

---

## Demo products that exercise all eight

**AHS — agentic hotel stay (new product `hotel_stay@1`, events `RESERVATION_CREATED`, `ROOM_PREP`).** Guest agent (CUSTOMER kind, acting for guest G) calls `reservations.create` through the gateway; the hotel dot (PLATFORM kind) picks up the reservation, reads the profile under `PREPARE_STAY` (PII, fields limited), requests `ASSIGN_ROOM 1802` → tier rule says top floor is for a higher tier → `PROPOSE 1702`; the dot accepts; `SET_ROOM_HVAC` and `HOUSEKEEPING_ADD` complete at T2; a second agent claims 1702 concurrently and gets `PROPOSE`; an ops manager later rejects a sibling approval → `RELEASE_ROOM` compensates. The pane shows guest agent, dot, four backends, one denial, one compensation.

**ABC — agentic benefits change (extends `contribution_change@1`).** Participant's agent asks to raise contribution to 8%; `benefits.participant.read` under `MANAGE_MY_BENEFITS` (PII, PHI denied); recent bank change + amount over `t1_limit` → `PENDING_APPROVAL` with `plan_admin` approver and 24 h SLA; agent attempting to approve its own request is rejected; admin approves; `CHANGE_CONTRIBUTION` dispatched; a `REVERT_CONTRIBUTION` is available within the window. Procurement-style cross-purpose read is demonstrated on the same agent with a second task to show isolation.

Both run against the local stack and the AWS sandbox (`RTDP_INGRESS`).

## Devin prompt (paste as-is)

```
Repo: rtdp. Read docs/design.md v2.1, docs/detailed-design.md, AGENTS.md, docs/demo-benefits.md and
docs/RTDP_v2.2_Agent_Control_Plane_Spec.md (this document, commit it first) before any change.

GOAL: Implement RTDP v2.2 Agent Control Plane: ADR-013..020 as specified, with gates G8..G15 passing
locally and on the AWS sandbox, plus the two demo products (hotel_stay@1, contribution_change@1 ext).
All data synthetic. No real systems. No public URL. Existing gates G1..G7 and the benefits/receipts
suites must keep passing unchanged.

CHANGE BOUNDARIES
- New services: services/agent-gateway (Go, MCP streamable-HTTP + REST), services/approval-service,
  services/agent-registry. New proto under proto/rtdp/agent/. New migrations under
  internal/store/migrations. New contracts under contracts/agent/. Seeds under assets/seed/agent/.
- Allowed edits to existing code ONLY for: envelope/proto Actor -> DelegationChain (ingress,
  orchestrator); PENDING_APPROVAL + PROPOSE outcomes and tier resolution (orchestrator);
  COMPENSATING state + compensating intents (action-dispatcher); CEL variables actor.*, read.*,
  timer.approval_due_at (rules-service); policy compile checks for owner/tiers/compensate_with
  (control-plane). Anything else: STOP and write the gap to docs/validation/v22-gaps.md.
- Write one ADR per gap in docs/adr/013..020 (Context, Decision, Consequences, Gate).
- Push after each ADR lands with its gate green. Never weaken an existing test.

ORDER
1. ADR-013 delegation chain + agent-registry + revocation (G8)
2. ADR-014 autonomy tiers, PENDING_APPROVAL, approval-service, owner on policy (G9)
3. ADR-015 agent-gateway with the tool set in the spec; mutating tools only via /v1/decide;
   CI static check that gateway has no backend write path (G10)
4. ADR-016 read grants, data classes, task context manifests, cross-purpose denial (G11)
5. ADR-017 PROPOSE, resource_claim, precedence policy (G12)
6. ADR-018 compensation catalogue, COMPENSATING state, retention + purge (G13)
7. ADR-019 agent_call facts, projections, Agents tab in demo UI (G14)
8. ADR-020 metering + work_completed_by_agent + done_right in eval report (G15)
9. Demo products AHS and ABC; tools/demo/agent_demo.py narrated run; tests/e2e/test_agent_control_plane.py
   covering G8..G15; docs/demo-agents.md; docs/validation/v22-gates.json.

AWS
- Deploy new services to EKS via the existing ArgoCD app-of-apps; new MSK topics agent.calls,
  agent.grants, approval.requests (rf=3, min.insync=2); Aurora migrations; Valkey for revocation sets,
  quotas and task-context keys hash-tagged {tenant:task}; KMS asymmetric key for agent token
  signing; Secrets Manager + IRSA for brokered backend credentials; SLA timers via the existing
  Flink timer path (fallback: EventBridge Scheduler). No new public endpoints; everything through
  the existing edge-tunnel. Record before/after image digests for the config-only tests
  (precedence flip in G12) in docs/validation/v22-config-only.json.

DONE WHEN: make up && make seed && python tools/demo/agent_demo.py runs end to end; pytest passes
including tests/e2e/test_agent_control_plane.py; G1..G15 green locally and against RTDP_INGRESS on
the sandbox; docs/adr/013..020 exist; v22-gates.json and v22-config-only.json committed.
```

## Guardrails for showing it

Show it from your own screen; share the GitHub repo, never the tunnel URL. Say "Client A / Client B", "Guest G", "the hotel", never a real brand. Keep performance claims as "measured on the synthetic stack." Frame it as how you think about an agent control plane, not as a recommendation to replace anything a prospective employer runs.
