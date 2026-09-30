# RTDP demo script — presenter runbook

A 10-minute walkthrough of the platform and the benefits product line.
Everything is synthetic. Two surfaces: the browser demo page (public URL
or localhost) and terminal commands.

## Setup (pick one)

- **Public URL:** https://ancient-implemented-olympic-fancy.trycloudflare.com
  — nothing to install. If it 502s, the tunnel pod restarted:
  `kubectl -n rtdp logs deploy/edge-tunnel | grep trycloudflare`
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

## Act 5 — a live plan change, no deploy

```bash
python tools/demo/benefits_live_change.py
```

> "Client B's plan went 250 → 400 — a compiled bundle and a new activation
> epoch. Same scenario flips REVIEW → APPROVE, then rolls back. Twelve
> image digests and the migration set are proven identical."

Evidence written to `docs/validation/phase3-config-only.json`.

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
- AWS ingress → `kubectl -n rtdp port-forward svc/ingress 8080:8080`
- Local stack → `make up && make seed`
- Scenario details → `docs/demo-use-cases.md`, `docs/demo-benefits.md`
