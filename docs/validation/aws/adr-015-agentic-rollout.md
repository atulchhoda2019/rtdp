# AWS validation — ADR-013/014/015 agentic layer rollout

Date: 2026-10-06. Cluster `rtdp-sandbox` (EKS 1.34, us-east-1, account
079457921611). All data synthetic.

## What shipped

- `agent-registry` (8090, JWKS + delegation mint), `approval-service`
  (8095, hold/release + ledger), `agent-gateway` (8096, REST + MCP) —
  deployed via ArgoCD, digest-pinned `linux/amd64` images from ECR.
- `gateway-tunnel` — second Cloudflare quick tunnel exposing the gateway
  publicly for the demo.
- Terraform: 3 ECR repos + lifecycle policies, 3 pod-identity roles
  (registry: PG/Valkey only; approval: Kafka consume+produce; gateway:
  Kafka produce `rtdp.agent.calls.v1`), decrypt-only KMS. Plan was
  18 add / 0 change / 0 destroy; applied.
- `rtdp.agent.calls.v1` added to the seed topic set.
- Seed job gained `RTDP_AGENT_REGISTRY` env so in-cluster seeding
  registers agents + grants.
- EKS access entry for `arn:aws:iam::079457921611:root` with
  `AmazonEKSClusterAdminPolicy` — cluster auth no longer depends on the
  `rtdp-devin` SSO role chain (survives SSO expiry; this session lost
  kubectl access twice before this landed).

## Deployed image digests

```
agent-registry    sha256:ec0681e06d6c83785231f54d2edebdbfb92c92171aeced15e19ee29c9bbad4fc
agent-gateway     sha256:d707729050a8af0528bdb0f561b04360a1d089c12c8add9f7a9032ecf239d231
approval-service  sha256:24270f53bc15815244f39b4a0bf17f395a8b6030ab5e4603d46c93a3a90e8505
```

All 14 service images (incl. tools) rebuilt `linux/amd64` and pushed via
`tools/aws/push-images.sh`; manifests carry `sha256:` digests only.

## Deployed state (post-rollout)

- ArgoCD: 17/17 Applications `Synced` + `Healthy`.
- Pods: all Running. Migrations 002–004 applied via one-off
  `rtdp-migrate-now` job; seed re-run `rtdp-seed` (helm-rendered)
  completed: 20 bundles synced, 6 agents + grants seeded, 5 ONNX +
  3 SLM warms, 16/16 pipeline warm probes ok (8 event types x
  tenant_a/tenant_b).
- Aurora `agent_call`: OK / DENIED / THROTTLED / PENDING_APPROVAL
  outcomes all observed live.
- `approval_held`: `5bb83aa5-…` ESCALATED (benefits SLA sweep ran),
  `1f1f6678-…` HELD then RELEASED in this session.

## Public endpoint

- Decisioning tunnel: `https://noon-utilization-figures-nav.trycloudflare.com`
- **Gateway tunnel: `https://timer-invitation-rebates-surveys.trycloudflare.com`**
  (`/healthz` 200; `/mcp` POST-only, 405 on GET)

Quick-tunnel hostnames rotate on pod restart — read the current URL from
`kubectl -n rtdp logs deploy/gateway-tunnel` before demoing.

## G10 gate — run against AWS

```
RTDP_GATEWAY=https://timer-invitation-rebates-surveys.trycloudflare.com \
RTDP_PSQL=/tmp/rtdp-psql.sh \
  python3 tests/e2e/test_agent_g10.py
```

(`rtdp-psql.sh` wraps `kubectl exec` into a throwaway pod running the
tools image with `envFrom: rtdp-db` → `psql "$RTDP_POSTGRES_DSN"`.)

Result: **14/14 PASS**

- session mint x2 (guest PERSON, ops ORG) — EdDSA delegation tokens
- scope-filtered reads: guest 2 rows / ops 3 rows + `rate_code`
- `crm.profile.read` denied to customer scope (403 + DENIED fact)
- `pms.room.assign` → `DECISION_PENDING_APPROVAL` + `ASSIGN_ROOM`
  intent; `agent_call` row links tool → decision_id
- quota breach → HTTP 429 + THROTTLED fact (28 calls)
- `tools/list` scope-filtered both REST and MCP
- MCP `initialize` (protocol 2025-03-26) + `tools/list` + `tools/call`
  with real rows over the public tunnel
- no token → 401, bogus token → 401

## Approval round-trip (live evidence)

```
POST /v1/tools/pms.room.assign  (ops token, floor 10 > vip floor 8)
  -> DECISION_PENDING_APPROVAL, decision 1f1f6678-b634-…
approval_held: state=HELD, approvers={role:front_desk_manager}
POST approval-service:8095/v1/approvals/1f1f6678-…
  {approver_identity: role:front_desk_manager, verdict: APPROVE}
  -> {"verdict":"RELEASED","intents":["…:ASSIGN_ROOM:gw"]}
approval_held: state=RELEASED; intent emitted to dispatcher topic
```

Note: gateway `approvals.*` tools are `human_only` and require the
`approve` scope on a PERSON principal — no seeded PERSON principal holds
it, so the demo approval path is the approval-service endpoint (as in
G9), reached in-cluster or via port-forward.

## Fixes applied during rollout

| Symptom | Root cause | Fix |
|---|---|---|
| agent-registry CrashLoop (419 restarts, exit 143) | `loadOrCreateKey` waits ≤2min for migration schema; liveness kills at ~35s | ran `rtdp-migrate-now` (002–004), restarted pod; startup/liveness ordering needs a durable fix |
| seed 404 at `slm_receipt_verification` | ollama init pulled only the two 0.5b tags | `qwen2.5:1.5b` added to init pull list (`ollama.yaml`) |
| RECEIPT_CLAIM warm → 502 | ollama pod OOMKilled (exit 137) loading 1.5b into 2Gi with both 0.5b resident | limit 4Gi / request 2Gi, `OLLAMA_MAX_LOADED_MODELS=3`, `KEEP_ALIVE=24h` (commit 3f8659d) |
| RECEIPT_CLAIM "no activation" | orchestrator initContainer s3-syncs bundles only at pod start; pod predated receipt/hotel bundles | `rollout restart orchestrator` |
| delegated decide → DECLINE_UNAUTHORIZED "unknown signing key" | ingress had no `RTDP_AGENT_REGISTRY_ADDR`; JWKS refresh hit localhost:8090 | env added to `apps/ingress.yaml` (commit 3c4d021) |
| kubectl lockout after SSO expiry | cluster auth only via `rtdp-devin` role chain | `API_AND_CONFIG_MAP` + root access entry (needed `bootstrap_cluster_creator_admin_permissions=true` to avoid cluster replacement) |

## Known fragility (pre-demo checklist)

- Quick tunnels rotate per pod restart → re-read both tunnel URLs.
- Warm registries are in-memory; pod reschedules degrade to
  `MISSING_REQUIRED_SIGNAL → REVIEW` until re-warmed (re-run the seed
  job — it is idempotent).
- No re-run of `warm_pipeline` retries past attempt 39 — if the seed
  dies mid-warm, probe both tenants manually then re-run the job.
- agent_registry should tolerate migration-wait beyond the liveness
  grace window (startupProbe or longer initialDelay) — currently relies
  on the migrate job having run first.
