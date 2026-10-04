# ADR-013 — Agent identity and delegation

Status: accepted (RTDP v2.2)
Gate: G8

## Context

The v2.1 actor model is a single caller identity: an authenticated
client id maps to a tenant at ingress, and nothing else about *who acted
for whom* survives into the decision fact. The agent control plane needs
a verifiable delegation chain — `principal → agent → sub-agent(s)` —
that rides the transaction envelope end to end so rules, arbitration,
approvals, and audit can reason about the actor (spec:
`docs/RTDP_v2.2_Agent_Control_Plane_Spec.md` §ADR-013).

## Decision

**Envelope.** `AuthenticatedTransaction` gains an optional
`rtdp.agent.v1.DelegationChain delegation` field. A chain has a
`principal { kind, id, tenant_id }`, ordered `links` (one per hop, each
carrying `agent_id`, `agent_version`, `agent_kind`, `scopes[]`,
`issued_at`, `expires_at`, `grant_id`), and a `proof` JWS (EdDSA,
`EdDSA` alg, `kid` selects the verification key). The chain travels with
`DecisionResult` so the durable fact is self-describing (G8a).

**Transport.** Callers pass the token in the `delegation_token` field of
`POST /v1/decide` (ingress copies it into `transaction.delegation`).
Absent token = legacy service caller — the pre-v2.2 path, unchanged.

**Registry.** New service `services/agent-registry` (REST) owns agents,
agent credentials, delegation grants (`principal → agent`, scoped,
TTL'd), revocations, and EdDSA signing keys. It mints chain tokens via
`POST /v1/delegations/mint`. Schema: `002_agent.sql`. Revocations are
persisted to `agent_revocation` **and** pushed into a shared Valkey set
`rtdp:agent:revoked` (grants, credential ids, and signing kids share one
namespace — `agt_`, `cr_`, `ak_` prefixes can't collide).

**Ingress validation.** On a delegation-bearing request, ingress:
fetches the JWKS document (60 s cache, refetched on unknown `kid`),
verifies the signature (G8a invalid-signature rejection), rejects
expired links (G8a), checks every `grant_id` and the signing `kid`
against the Valkey revocation set (G8b), and enforces the tenant's
`max_chain_depth` (G8c). Any failure → HTTP 401. Legacy (no-chain)
requests are unaffected.

**Scope enforcement.** Scope is product data, so the check runs in the
orchestrator *after* activation resolves the pinned product: if
`effective_config.required_scope` is set and the transaction carries a
chain, **every link** must include the scope (delegation narrows, never
widens). Failure → `DECISION_DECLINE_UNAUTHORIZED` with reason
`DELEGATION_SCOPE_INSUFFICIENT:<agent_id>`; the decision fact is still
committed durably (evaluation is skipped, the fact is not). Products
without `required_scope` are unaffected.

**Rules.** The last-hop agent and principal are exposed to CEL as
`actor.principal.kind`, `actor.principal.id`, `actor.agent.id`,
`actor.agent.kind`, `actor.agent.scopes`, `actor.chain_depth`. No chain
→ the keys are absent (null in CEL) — existing rulesets compile and
behave unchanged.

**Suspension.** Setting an agent's status to non-ACTIVE revokes all its
live credentials **and** grants (both published to the revocation set),
so its next delegated call is denied at the edge.

## Consequences

- Positive: every delegated decision is attributable; revocation is a
  distributed-set check (sub-ms, propagates at write time — G8b's ≤5 s
  bound is met in ~0 s); scope checks stay config-driven (`required_scope`
  is bundle data, not code).
- Cost: one extra Redis `SMEMBERS`-style check per delegated request;
  JWKS is cached. Legacy requests pay nothing.
- Limitation: ingress trusts the registry's JWKS endpoint; a registry
  outage fails closed only for delegated traffic (cached keys still
  verify). Depth enforcement is per-tenant config, not per-principal.
- Follow-on: ADR-016 read grants, ADR-019 `agent_call` facts build on
  this chain; the gateway (ADR-015) mints tokens, callers never sign.

## Gate — G8

- (a) valid chain → `actor.*` in rule input + `DecisionResult.delegation`;
  invalid signature/expiry → 401 at ingress.
- (b) revoke grant → next request denied; propagation measured (≤5 s,
  expected ~0).
- (c) chain length > `max_chain_depth` → 401.
- (d) contribution_change@1 declares `required_scope: benefits`; a chain
  missing it → `DECISION_DECLINE_UNAUTHORIZED`; the fact persists.

Checks: `tests/e2e/test_agent_g8.py` (local, and against `RTDP_INGRESS`
on the sandbox).
