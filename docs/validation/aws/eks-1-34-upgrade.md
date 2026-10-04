# EKS extended-support exit — 1.31 -> 1.34 upgrade

2026-10-04 — `rtdp-monthly-cap` budget alert fired: $101.18 of $200
with ~$32/day burn. Cost Explorer showed `rtdp-sandbox` had flipped to
EKS **extended support** (~Oct 3): `AmazonEKS-Hours:extendedSupport
$38.00` vs `perCluster $7.60` MTD. Extended support is $0.60/hr vs
$0.10/hr standard — a **~$360/mo** uplift, growing forward.

Forward run-rate was ~$44/day ≈ $1,300/mo. Fix: upgrade the control
plane to a standard-support version. Standard versions at the time:
1.34, 1.35, 1.36, 1.37. EKS allows single-version hops only, so the
upgrade ran 1.31 -> 1.32 -> 1.33 -> 1.34 (three sequential
`terraform apply`s), then managed node groups rolled to 1.34.

## Commands

```bash
cd infra/terraform/envs/sandbox
export AWS_PROFILE=rtdp-devin
terraform init
terraform plan    # posted: 0 add / 1 change / 0 destroy — version only
terraform apply   # hop 1: 1.31 -> 1.32
# (bump kubernetes_version, repeat for 1.33, then 1.34)
aws eks update-nodegroup-version --cluster-name rtdp-sandbox \
    --nodegroup-name system --kubernetes-version 1.34
aws eks update-nodegroup-version --cluster-name rtdp-sandbox \
    --nodegroup-name cpu-inference --kubernetes-version 1.34
```

## Cost impact

| Line | Before | After |
|---|---|---|
| EKS control plane | $0.60/hr (extended) | $0.10/hr (standard) |
| Monthly delta | ~$432/mo | ~$72/mo (**−$360/mo**) |

Remaining forward burn ≈ $20–25/day ≈ $700/mo — still above the $200
budget; the sandbox stack cannot fit $200/mo as a 24/7 deployment.

## Verification

- `aws eks describe-cluster` -> version 1.34, upgradePolicy standard
- Node groups on 1.34, pods Running, demo request green post-upgrade
- `terraform plan` clean after (no drift)

## Results (2026-10-04, post-upgrade)

Apply durations: 1.31->1.32 = 7m9s, 1.32->1.33 = 6m59s,
1.33->1.34 = 7m8s; upgrade_policy STANDARD apply ~2s. Managed
node-group rolls: `system` then `cpu-inference`, sequential rolling
replacement, both ACTIVE on 1.34.

```text
cluster:    v=1.34  support=STANDARD  status=ACTIVE
nodegroups: system=v1.34 ACTIVE   cpu-inference=v1.34 ACTIVE
nodes:      v1.34.11-eks-3b4a6ca (all; zero v1.31 remaining)
pods:       12/12 Running in rtdp ns
terraform:  fmt clean, validate ok, plan "No changes"
demo:       3/3 POST /v1/decide -> DECISION_APPROVE via public URL
```

## Post-roll findings

1. **Edge tunnel URL rotated.** `edge-tunnel` rescheduled onto a new
   node and cloudflared minted a new quick-tunnel hostname
   (`noon-utilization-figures-nav.trycloudflare.com`). The previous URL
   is dead — expected with ephemeral trycloudflare tunnels; the durable
   in-cluster endpoint remains an open item.
2. **Inference warm registry is pod-local.** `inference-service` keeps
   warmed ONNX models in memory; the pod reschedule wiped them, so
   `Score` returned UNAVAILABLE -> `MISSING_REQUIRED_SIGNAL` -> REVIEW
   (correct degraded behavior, but the demo needs warmed models).
   Resolution: re-created the `rtdp-seed` spec as one-off job
   `rtdp-rewarm` (same pinned `rtdp/tools` digest, `rtdp-bootstrap` SA).
   Run log: 5 ONNX warms + 2 SLM warms green, 14/14 pipeline-warm calls
   ok on attempt 1, `seed complete`. Follow-up: warm-on-start or an
   idempotent rewarm CronJob would remove this manual step.
3. **Node count settled at 2** (one t3.large + one c7i.xlarge) — the
   surge nodes seen mid-roll are gone.

## Remaining risk

Burn is still ~$700/mo vs the $200 cap — the upgrade removed the
extended-support multiplier only. Right-sizing or ephemeral teardown
are separate decisions.
