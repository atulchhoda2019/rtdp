# Stages C+D — data services + compute (PASS)

Date: 2025-09-29. Account: 079457921611. Region: us-east-1. Role: rtdp-devin.

## Applied (72 add, 3 converge passes)

| Module | Shape (sandbox-sized) | Status |
|---|---|---|
| msk | 3 × kafka.t3.small, IAM+TLS, KMS | ACTIVE |
| aurora | PG 16.15, db.t4g.medium writer only, Secrets Manager master | available |
| elasticache | Valkey 8.0, 1 × cache.t4g.micro, TLS+KMS | available |
| eks | v1.31, coredns/vpc-cni addons, 1×t3.large system + 1×c7i.xlarge inference | ACTIVE |
| flink | rtdp-features-sandbox, FLINK-1_20, parallelism 1, S3 checkpoints | READY |
| iam_workloads | 11 per-service roles + Pod Identity associations | created |
| network | +4 interface endpoints (elasticache/rds/eks; kafka dropped) | available |

Post-apply `terraform plan`: **no changes** (zero drift).

## Fixes applied during apply

- `iam:PassedToService` now includes `pods.eks.amazonaws.com` (Pod Identity
  associations were denied without it — RTDPDevinPolicy v2).
- kafka interface endpoint removed — MSK is reached directly in-VPC; its
  endpoint service requires PrivateLink acceptance anyway.
- aurora engine 16.4 -> 16.15 (16.4 not offered in us-east-1); parameterized.
- flink role gets kms:Decrypt/GenerateDataKey on data+artifacts CMKs —
  jar bucket is SSE-KMS; app creation validated the object and failed without it.
- eks + flink roles now carry the permission boundary (RolesMustCarryBoundary
  deny would have blocked them).

## Sleep/wake (tools/aws/)

- `make aws-sleep` — stop Aurora, stop Flink app, scale node groups to 0.
  Aurora auto-restarts after 7 days (AWS limit). Residual ~$295/mo.
- `make aws-wake` — reverse. "Devin wake up" / "Devin provision".
- `make aws-teardown` — CONFIRM=teardown destroys C+D; ~$85/mo Stage-B floor.

## Cost

~$530/mo for C+D (~$615 all-in with B), right-sized from ~$1,150.
Signup credits (~$200) absorb initial burn; budget cap alerts at 50/80/100%.
