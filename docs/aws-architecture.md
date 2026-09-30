# RTDP on AWS — as-deployed architecture (sandbox)

Companion to `docs/design.md` v2.1 (behavior) and `docs/aws-deployment.md`
(service mapping + guardrails). This document describes what is actually
running in the sandbox account — topology, identities, and the public demo
path — not what was planned.

Account `079457921611` · region `us-east-1` · profile `rtdp-devin`
(role `RTDPDevinRole` via SSO `rtdp-sso`) · all data synthetic.
Live evidence: `docs/validation/aws/stage-f.md`.

## 1. Topology

```
                       public demo path (account-less Cloudflare quick tunnel)
  browser ── https://*.trycloudflare.com ──► edge-tunnel pod ──► ingress:8080
                                                   (outbound QUIC only;
                                                    no inbound listener,
                                                    no ELB — the permission
                                                    boundary denies
                                                    iam:CreateServiceLinkedRole)
                       operator path
  kubectl port-forward svc/ingress 8080 ──► ingress:8080

  EKS rtdp-sandbox (k8s 1.31) · namespace rtdp
  ┌─────────────────────────────────────────────────────────────────────┐
  │  ingress ──► orchestrator ──┬─► feature-service ──► Valkey (tier1+2) │
  │   (demo UI at GET /)        ├─► signal-resolver ─┬─► inference (ONNX) │
  │                             │                  └─► slm-service ──► ollama
  │                             ├─► rules-service (CEL)                  │
  │                             └─► MSK txn: decision.facts + contrib +  │
  │                                  action.commands + egress            │
  │  projector ◄── decision.facts ──► Aurora (projected facts)          │
  │  action-dispatcher ◄── action.commands ──► adapters ──► Aurora ledger│
  │  Managed Flink app ◄── feature.contrib ──► feature-materializer ──► Valkey
  └─────────────────────────────────────────────────────────────────────┘
  ArgoCD (ns argocd): rtdp-apps root → 13 child apps · git = sole source
```

## 2. Compute

- **EKS `rtdp-sandbox`** — Kubernetes 1.31. Node pools: system + `cpu-inference`
  (tainted `rtdp.io/inference=cpu:NoSchedule`). Ollama and edge-tunnel run on
  the inference pool; everything else on system.
- **Deployments** (ns `rtdp`, all ClusterIP): ingress, orchestrator,
  feature-service, signal-resolver, rules-service, inference-service,
  slm-service, ollama, action-dispatcher, projector, feature-materializer,
  edge-tunnel.
- **ECR** `079457921611.dkr.ecr.us-east-1.amazonaws.com/rtdp/<svc>` — images
  are digest-pinned (`repo@sha256:…`) in the ArgoCD app manifests and built
  for `linux/amd64`.
- **Managed Flink** application `rtdp-features-sandbox` — event-time tiles
  from `rtdp.feature.contrib.v1`, checkpoints in `rtdp-checkpoints-*`.

## 3. Data plane

| Store | Resource | Role |
|---|---|---|
| Amazon MSK `rtdp-sandbox` | 11 `rtdp.*` topics, **rf=3, min.insync.replicas=2**, IAM auth `:9098` | Commit boundary — one txn commits decision.facts + feature.contrib + action.commands + egress |
| Aurora PostgreSQL `rtdp-sandbox` | writer `rtdp-sandbox.cluster-cc7sccyi262s…` | Projected facts, action intents, execution ledger |
| ElastiCache **Valkey 9.0** `master.rtdp-sandbox.gzl9h2…`, param group `rtdp-valkey9` | TLS | Tier-1 Lua dedup/counters (`HEXPIRE` field TTL — requires ≥9.0 server-side here) + tier-2 tiles |
| S3 ×4 + KMS | `rtdp-artifacts-`, `rtdp-bundles-`, `rtdp-checkpoints-`, `rtdp-validation-` `-079457921611` | Model artifacts, compiled bundles, Flink checkpoints, evidence |

## 4. Identity (EKS Pod Identity, no IAM users)

Per-service roles in `infra/terraform/modules/iam-workloads`, boundary-attached:

- `orchestrator`, `action-dispatcher` — MSK transactional write
  (`WriteTxnMarkers` at cluster scope included), `pg-connect`
- `feature-service`, `feature-materializer` — `kafka-read-features`, `valkey`
- `projector` — `kafka-read-decisions`, `pg-connect`
- `inference-service`, `slm-service` — **read-only** `s3-artifacts` + nothing
  else (ADR-011 enforced at IAM, not convention)
- `rtdp-bootstrap` — topic admin (`Describe/AlterTopicDynamicConfiguration`
  at cluster scope), seed uploads to artifacts/bundles

## 5. GitOps and bootstrap

`rtdp-apps` (app-of-apps) owns 13 child Applications; each points at this
repo `main` and stamps a digest-pinned image. PostSync hooks run `rtdp-topics`
(topic ensure/`RTDP_TOPIC_DESCRIBE=1` evidence mode), migration, and
`rtdp-seed` (models → S3, compiled bundles → S3, `aws-sandbox` activations,
warm pipeline). Seed warmup transaction ids carry a per-run uuid nonce so
re-seeding is idempotent, not a dedup conflict.

## 6. Demo surface

- `GET /` — embedded scenario-driven demo page (nine use-case buttons +
  manual request form + run log).
- `POST /v1/decide` — JSON decision API; tenant from `X-RTDP-Client-Id`
  (`demo-client-a`/`demo-client-b`).
- Public URL rotates when edge-tunnel restarts:
  `kubectl -n rtdp logs deploy/edge-tunnel | grep trycloudflare`.
- Known edge behavior: Cloudflare masks upstream 502 bodies (dedup CONFLICT
  shows as bare 502 publicly; full body via port-forward).
- `RTDP_DECIDE_TIMEOUT_MS=35000` on ingress — bound only; the product
  deadline governs (100ms fast path, 15s document-intake SLM path).

## 7. Cost posture

~$530/mo while running (EKS, MSK ×3 brokers, Aurora, Valkey, NAT).
`make aws-teardown` reduces to ~$85/mo (stops Flink, scales nodes).
EKS 1.31 extended support ends 2026-11-26 — irrelevant if torn down before;
otherwise bump `cluster_version` to 1.35.
