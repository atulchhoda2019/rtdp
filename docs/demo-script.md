# RTDP demo script — presenter runbook

A 10-minute walkthrough of the platform and the benefits product line.
Everything is synthetic. Two surfaces: the browser demo page (public URL
or localhost) and terminal commands.

## Setup (pick one)

- **Public URL:** https://noon-utilization-figures-nav.trycloudflare.com
  — nothing to install. If it 502s, the tunnel pod restarted:
  `kubectl -n rtdp logs deploy/edge-tunnel | grep trycloudflare`
- **Agent gateway (MCP):**
  https://timer-invitation-rebates-surveys.trycloudflare.com — separate
  tunnel; if it dies:
  `kubectl -n rtdp logs deploy/gateway-tunnel | grep trycloudflare`
- **Localhost:** `kubectl -n rtdp port-forward svc/ingress 8080:8080`
  → `http://localhost:8080` (AWS), or `make up && make seed` for the
  fully local stack.

## Act 1 — a decision happens

Open the URL. Click **Standard claim**.

> "Every request flows ingress → policy → features → model → rules →
> durable commit → action intent. That bundle digest is the pinned policy
> version that answered — replayable coordinates."

Point at: outcome badge, reason codes, `bundle_digest`, `manifest_epoch`.

## Act 2 — deterministic guarantees

- **Velocity attack**: 13 submits; 1–12 pass, **#13 DECLINE/VELOCITY_LIMIT**
- **Duplicate retry**: identical `decision_id` — replay, not re-count
- **Tampered replay**: mutated amount on a used txn id → rejected
  (bare 502 over the tunnel; full CONFLICT body via port-forward)
- **Rogue tenant**: → 401, rejected before any decision work
- **Tenant A vs B**: same claim, different pinned `bundle_digest`s

> "Tier-1 counters, dedup, and auth-bound tenant identity — deterministic,
> every time."

## Act 3 — AI as a bounded signal provider

Click **Document intake (SLM)** — allow a few seconds.

> "An SLM scores narrative consistency and emits a typed signal on a
> contract — pinned prompt, deadline, schema-checked output. Rules decide;
> the model can't approve anything."

## Act 4 — benefits as configuration (the new part)

Terminal, from the repo root:

```bash
python tools/demo/benefits_demo.py --only hsa      # $320 receipt, A vs B
python tools/demo/benefits_demo.py --only dep      # SLM doc consistency
python tools/demo/benefits_demo.py --only contrib  # UNKNOWN effect path
```

> "Client A auto-approves the same receipt that Client B routes to review
> — only an allowlisted `thresholds.*` overlay differs. Dependent
> verification has no DECLINE action at all — AI approves, humans handle
> the rest. And the contribution adapter times out *after* a possible
> apply: the effect lands UNKNOWN and policy says reconcile, never retry."

## Act 4b — receipt claims at scale (docling → rules → BPO math)

```bash
.venv-docling/bin/streamlit run tools/demo/receipt_app.py
```

Upload or pick a corpus receipt — watch docling extract the lines, then
submit the claim. Clean receipt claiming the eligible subtotal →
**DECISION_APPROVE**. Then try `demo-client-b` — the same claim reviews
under the $50 overlay.

> "OCR is commoditized — docling runs on CPU. The expensive part was the
> LLM checking deterministic rules, so the rules do that for free and the
> model only scores document-vs-claim consistency. AI never rejects:
> there is no decline path in the action policy, and a test walks every
> outcome to prove it. Everything else lands in the review queue with the
> extracted fields pre-filled."

Then the aggregate evidence:

```bash
cat docs/validation/receipts-at-scale.json
```

> "Measured against the Textract-plus-LLM-plus-BPO counterfactual: ~47%
> cheaper per document at a million documents, 94% of inflated claims
> caught by deterministic rules — the model never touches a line item —
> and every decision carries the bundle digest you'd replay to audit it."

## Act 5 — a live plan change, no deploy

```bash
python tools/demo/benefits_live_change.py
```

> "Client B's plan went 250 → 400 — a compiled bundle and a new activation
> epoch. Same scenario flips REVIEW → APPROVE, then rolls back. Twelve
> image digests and the migration set are proven identical."

Evidence written to `docs/validation/phase3-config-only.json`.

## Act 5b — governed agents (MCP)

Gateway: `https://timer-invitation-rebates-surveys.trycloudflare.com`

```bash
GW=https://timer-invitation-rebates-surveys.trycloudflare.com

# 1. Mint a delegation — the caller never holds a key
TOK=$(curl -s $GW/v1/session -X POST -H 'Content-Type: application/json' -d '{
  "principal_kind":"ORG","principal_id":"hotel-h","tenant_id":"tenant_a",
  "agent_ids":["hotel-ops-dot"]}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["delegation_token"])')

# 2. MCP: initialize + scope-filtered tools/list
curl -s $GW/mcp -X POST -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}'
curl -s $GW/mcp -X POST -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | python3 -m json.tool

# 3. A mutating tool routes through the governed decide path
curl -s $GW/v1/tools/pms.room.assign -X POST -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' -d '{"task_id":"demo-1","purpose":"PREPARE_STAY",
  "input":{"attributes":{"reservation_id":"res-9001","room_id":"r-1204","preferred_floor":10}}}'
#  -> DECISION_PENDING_APPROVAL: floor 10 exceeds the VIP auto-assign floor.
```

> "The agent authenticates with a delegation chain — signature, tenant,
> depth, revocation — not a bearer API key. Its tool list is its scope
> list. A mutating call can't touch a backend: it becomes a decision
> request, pinned to the same bundle digest as everything else, and this
> one held for a human because the policy says so. Every call is a
> fact in `agent_call` — OK, DENIED, THROTTLED, PENDING — replayable."

Approval release (in-cluster — the human approver path):

```bash
kubectl -n rtdp port-forward svc/approval-service 8095:8095 &
curl -s localhost:8095/v1/approvals/<decision_id> -X POST \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"tenant_a","approver_identity":"role:front_desk_manager","verdict":"APPROVE"}'
#  -> {"verdict":"RELEASED","intents":["…:ASSIGN_ROOM:gw"]}
```

A guest token minted for `guest-g`/`guest-agent-g` shows the other side:
`tools/list` returns only `decide`, `reservations.lookup`,
`reservations.create` — the ops toolset isn't even visible.

## Act 6 — it is all tested

```bash
make e2e-cases      # 11/11 platform guarantees
make e2e-benefits   # 15/15 benefits checks
```

> "Every claim in this demo is an assertion — the same suite ran green on
> AWS minutes ago."

## Closing

> "Models, prompts, thresholds, action policies, tenant plans — all
> pinned, replayable configuration. An entire benefits product line was
> added without touching a service. Decisioning as versioned config,
> with the audit trail to prove it."

## Troubleshooting

- Tunnel dead → `kubectl -n rtdp logs deploy/edge-tunnel | grep trycloudflare`
- Gateway tunnel dead → `kubectl -n rtdp logs deploy/gateway-tunnel | grep trycloudflare`
- AWS ingress → `kubectl -n rtdp port-forward svc/ingress 8080:8080`
- Local stack → `make up && make seed`
- Scenario details → `docs/demo-use-cases.md`, `docs/demo-benefits.md`
