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

### Act 5c — a real LLM drives the door (the money shot)

```bash
export ANTHROPIC_API_KEY=...   # or OPENAI_API_KEY, keys stay local
.venv/bin/python tools/demo/agent_client.py \
    --provider anthropic --persona ops --mode scenario
# swap --provider openai to run the same door with a different brain
# --mode repl for free-form Q&A, --persona guest for the scoped view
```

A scripted prompt sends the model through the same door: it mints its own
delegation, lists only what its scopes expose, calls `reservations.lookup`
+ `crm.profile.read`, then attempts `pms.room.assign` — which lands
`PENDING_APPROVAL`. The wire traffic prints under each call.

> "That was a real model deciding to call tools — not a script. It never
> sees a credential, only a scoped delegation. Its mutation didn't write
> anything; it produced a decision that policy holds until a human
> releases it. Swap Claude for GPT and nothing about the gate changes."

### Act 5d — end to end on four screens (decision → intent → effect)

The decision is not the finish line. This act runs the whole arc live:
the agent proposes, a human releases, the dispatcher executes, and the
world actually changes.

**Screen layout (4 panes):**

```
┌─────────────────────────────┬─────────────────────────────┐
│ 1. AGENT  (LLM + tools)     │ 2. EFFECT LEDGER (watcher)  │
│    agent_client.py          │    effect_watch.py          │
├─────────────────────────────┼─────────────────────────────┤
│ 3. HUMAN APPROVAL DESK      │ 4. WORLD (re-query)         │
│    approvals pending/release│    agent_client --mode repl │
└─────────────────────────────┴─────────────────────────────┘
```

**Setup:**

```bash
aws sso login --profile rtdp-sso          # kubectl legs need it once

# pane 3 — the human approver channel (in-cluster service)
kubectl -n rtdp port-forward svc/approval-service 8095:8095 &

# pane 2 — psql wrapper through a throwaway pod (once)
kubectl -n rtdp run rtdp-psql --restart=Never --image=postgres:16 -i \
  --env-from=secret/rtdp-db -- sleep 3600
cat > /tmp/rtdp-psql.sh <<'EOF'
#!/bin/sh
kubectl -n rtdp exec -i rtdp-psql -- \
  sh -lc 'psql "$RTDP_POSTGRES_DSN" -tAc "$0"' "$1"
EOF
chmod +x /tmp/rtdp-psql.sh
```

**The run:**

```bash
# pane 2 — start watching before the agent acts
RTDP_PSQL=/tmp/rtdp-psql.sh \
  .venv/bin/python tools/demo/effect_watch.py --latest

# pane 1 — the tuned local model drives (base qwen works too, weaker)
nohup .venv/bin/mlx_lm.server --model tools/agent_ft/fused --port 8088 &
.venv/bin/python tools/demo/agent_client.py \
    --provider openai --base-url http://localhost:8088/v1 \
    --model "$PWD/tools/agent_ft/fused" --persona ops --mode scenario
```

Pane 1 shows: `reservations.lookup` → `crm.profile.read` →
`pms.room.assign` → `PENDING_APPROVAL` (floor 10 > auto-assign floor 8).
Pane 2 shows the hold land: `approval_held … ASSIGN_ROOM HELD`.

```bash
# pane 3 — the human sees the queue and releases
curl -s 'localhost:8095/v1/approvals/pending?tenant_id=tenant_a' | python3 -m json.tool
curl -s localhost:8095/v1/approvals/<decision_id> -X POST \
  -H 'Content-Type: application/json' \
  -d '{"approver_identity":"role:front_desk_manager","verdict":"APPROVE"}'
```

Pane 2 then shows the release and execution live:
`approval_held → RELEASED`, `action_execution → DISPATCHING →
ACKNOWLEDGED ref=notify:…`.

```bash
# pane 4 — the payoff: ask the agent to look again
#   (REPL mode: "check res-9001 again")
# -> reservations.lookup now returns room_id=r-1204, status=ROOM_ASSIGNED
```

> "Three facts, not one: the decision, the intent, and the effect —
> committed separately. A human released the hold through a different
> service on a different identity, the dispatcher executed through its
> declared adapter with lease fencing and inbox dedup, and the world
> changed: ask the agent to look again and the reservation is assigned.
> That last lookup isn't a cache — it reads the action ledger."

### Act 5e — the sandbox around the agent (Strands Box)

Same run, contained. `tools/agent_box/` runs the agent client in an OS
sandbox where a Dogwood policy decides every connection: the workload
reaches exactly the governed gateway and the model endpoint — nothing
else. A temporal rule also caps gateway requests at 30/10min,
independent of the gateway's own quota.

```bash
~/box-core/box-core/box run --config tools/agent_box/box.toml
# try any other destination from inside: refused, deny-by-default
```

> "Three independent layers: the model decides what to do, the sandbox
> decides where the process can go, the gateway decides what the agent
> is allowed to do. A stolen token inside this box can't even dial out."

### Act 5f — we trained the discipline

```bash
.venv/bin/python tools/agent_ft/eval.py
# BASE    Qwen2.5-1.5B-Instruct: 2/12   — narrates, never calls tools
# ADAPTER iter-300:             10/12  — correct first call, refuses
#                                        to bypass holds
```

> "The agent's brain is also an artifact: a LoRA adapter trained on
> synthetic governed-agent traces, evaluated against held-out scenarios,
> digest-pinnable like everything else. Base model narrates an action it
> never performed; the tuned one calls the tool and reports the hold."

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
