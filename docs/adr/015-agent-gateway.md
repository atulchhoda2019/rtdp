# ADR-015: Governed agent gateway

## Context

Agents that act for principals need a way to invoke tools — look up a
reservation, assign a room, change a contribution — but the platform's
guarantees only hold if every invocation is authenticated, scoped,
rate-limited, and routed through the decision plane. Letting each agent
talk to backends directly would bypass `/v1/decide`, scatter audit data
across systems, and hand backend write credentials to arbitrary callers.

## Decision

A single governed door — `agent-gateway` (REST + MCP streamable HTTP,
:8096) — sits between agents and everything else.

**Identity.** Callers never sign tokens. They open a session through
`POST /v1/session`, which proxies the mint to `agent-registry`; the
registry enforces grant membership and returns an EdDSA delegation
token (ADR-013). Every subsequent call carries the token; the gateway
re-runs the full edge check — JWKS signature, expiry, tenant, depth,
Redis revocation — before dispatch.

**Tool surface.** Tools are config (`assets/seed/agent/gateway_tools.yaml`),
not code. Three kinds:

- `read` — synthetic read-only backends (reservations, CRM, benefits).
  Rows are filtered to the caller's principal unless a `all`-visibility
  scope is held; fields are allow-listed per caller class (customer vs
  employee).
- `decide` — mutating operations. The gateway translates the call into
  a decision request and forwards it to ingress `/v1/decide` under the
  tenant-bound client identity with the caller's delegation token.
  There is no other path: the gateway holds no backend write
  credentials, and the source tree contains no backend write client.
- `proxy` — governed internal services (approval-service). The gateway
  adds no privileges; the callee enforces its own rules.

**Enforcement order.** For every call: verify → required-scope check →
human-only check → calls/min quota → concurrency quota → per-tool
deadline → dispatch → durable `agent_call` fact. The fact records
call id, task/purpose context, principal, agent, chain depth, tool,
backend, outcome (`OK|DENIED|THROTTLED|ERROR|PROPOSED|
PENDING_APPROVAL`), decision linkage, latency, cost units, and data
classes read — inserted into `agent_call` (Postgres) and published to
`rtdp.agent.calls.v1`. ADR-019's pane reads these alone for the
agentic call graph.

**Scope filtering.** `GET /v1/tools` and MCP `tools/list` return only
tools whose `required_scope` the caller's chain carries — a guest agent
and a property-ops agent see different surfaces.

**Hotel demo vertical.** The synthetic `hotel_stay@1` product
(`ROOM_PREP`/`HVAC_SET`/`HOUSEKEEPING_TASK`/`RESERVATION_CREATED`
events, `ASSIGN_ROOM`/`SET_ROOM_HVAC`/`HOUSEKEEPING_ADD` intents)
exercises the decide path end-to-end with no real systems.

## Consequences

- Every agentic call is durable and auditable before it returns.
- Mutating tools inherit autonomy tiers (ADR-014): a held intent
  surfaces as `agent_call` outcome `PENDING_APPROVAL` with the
  decision linked.
- Quotas are per-agent per-tool, enforced in Valkey; breach returns
  HTTP 429 and a `THROTTLED` fact.
- Adding a tool is config — no rebuild, no migration.
- ADR-016 read grants slot into the read kind's field/row filtering;
  ADR-017 proposals change `decide` outcomes to `PROPOSED` with no
  gateway code change.

## Gate — G10

a. Customer (`reservations`) and employee (`reservations_admin`) agents
   call `reservations.lookup`; rows and fields differ correctly.
b. `pms.room.assign` produces a decision fact + `ASSIGN_ROOM` intent;
   the `agent_call` row links the two. Static check: gateway source has
   no write path outside `agent_call`.
c. Quota breach → HTTP 429 + `THROTTLED` fact.
d. Tool lists differ by scope, over REST and MCP.

Evidence: `docs/validation/adr-015-g10.md`.
