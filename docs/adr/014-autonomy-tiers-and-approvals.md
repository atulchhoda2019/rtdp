# ADR-014: Autonomy tiers and human approval

Status: implemented (G9). Builds on ADR-013's delegation chain.

## Decision

Every emitted action intent resolves an autonomy tier **per action**,
not per decision:

- **T2** — may complete alone (default when no tier is declared).
- **T1** — completes alone only when every declared CEL condition
  evaluates true over `input` / `cfg` / `actor` / `timer` (e.g.
  `input.amount <= cfg.t1_limit`).
- **T0** — never completes alone; always waits for a human.

The caller's **autonomy ceiling** caps the action's declared tier: a
delegated agent carries `max_autonomy` on its registry record, minted
into each delegation link and enforced at intent emission. An absent
delegation (legacy service caller) is platform T2 — unchanged behavior.

Held intents are emitted with `INTENT_AWAITING_APPROVAL` plus approval
metadata copied from the pinned action policy: `approvers`,
`escalation_approvers`, `sla_breach_action`, `approval_due_at`. The
metadata travels on the durable `ActionCommand`, so approval-service
needs no bundle resolution — a held command is self-describing.

A decision whose outcome is `APPROVE` with any held intent lands
`DECISION_PENDING_APPROVAL`; `APPROVE_WITH_APPROVAL` from a ruleset maps
to the same outcome and, when no intent was otherwise held, holds all
emitted intents so the decision is releasable. `REVIEW` / `DECLINE`
outcomes keep their own state — held intents under them still wait, but
the decision fact stays faithful.

## Components

- `approval_held` + `approval_event` tables (`003_approval.sql`).
- `approval-service` (`:8095`): consumes `rtdp.action.commands.v1`
  AWAITING_APPROVAL commands, exposes
  `POST /v1/approvals/{decision_id}` (`APPROVE`/`REJECT`) and
  `GET /v1/approvals/pending`, runs a 2 s SLA sweeper, and republishes
  `READY` / `CANCELLED` / `EXPIRED` commands. Every verdict is a durable
  `ApprovalEvent` on `rtdp.approval.events.v1` and in `approval_event`.
- `action-dispatcher`: holds AWAITING commands in `action_execution`,
  claims released `READY` commands across the `AWAITING_APPROVAL →
  DISPATCHING` transition, and marks `CANCELLED`/`EXPIRED` terminal.
  Inbox dedup is keyed `idempotency_key + status` so a release is a
  distinct message, not a duplicate.
- Compiler (`rtdp_contracts`): `owner` is required on every action
  policy (`MISSING_OWNER`); `autonomy_tiers` and per-action `actions`
  metadata are validated at compile time.

## Rules

- **Self-approval is barred**: the requesting agent and principal
  (carried as `requester_identities` on the command) may never approve
  their own request — enforced in approval-service, not convention.
- An approver must appear in the intent's `approvers` list (or
  `escalation_approvers` once escalated); role entries
  (`role:plan_admin`) are satisfied by the bare role name.
- A T2 action with no configured approvers that gets held (e.g. capped
  by a T1 agent ceiling) falls back to the policy owner identity —
  there is always an accountable human.
- SLA breach: `ESCALATE` re-routes to `escalation_approvers` once
  (24 h reset) then expires; `EXPIRE` terminates the intent immediately.

## Cost / limitations

- Cost: one extra field-level pass per emitted intent; held commands
  add one Kafka hop (release republish).
- Limitation: approver lists are role strings from config — no SSO or
  directory lookup (synthetic demo scope).
- Follow-on: ADR-016 read grants scope who may call the approval API;
  ADR-018 emits compensation actions when a held sibling is rejected.

## Gate — G9

- (a) T2 agent completes alone; held T1 does not block a ready T2;
  T1 agent ceiling caps a T2 action.
- (b) requesting agent and principal cannot self-approve; non-listed
  approver rejected (403).
- (c) listed approver releases → dispatcher reaches `ACKNOWLEDGED`;
  `REJECT` → `CANCELLED`, never dispatched.
- (d) SLA breach escalates to `escalation_approvers` (`PLAN_AUDIT`
  fixture: T0, `sla_seconds: 6`, `ESCALATE`).
- (e) action policy without `owner` fails compilation.

Checks: `tests/e2e/test_agent_g9.py`.
