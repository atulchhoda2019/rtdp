# Stage B — foundation apply (PASS)

Date: 2025-09-29. Account: 079457921611. Region: us-east-1. Role: rtdp-devin (assumed RTDPDevinRole).

## Applied

`terraform apply` — 81 add / 0 change / 0 destroy on first pass; second pass
(+20/-9) recreated ECR repos pinned to the artifacts CMK after the aws/ecr
managed key raced creation. Net: 90 managed objects.

- network: vpc-06a082a87cae062bd (10.40.0.0/16), 9 subnets (pub/priv/data x3 AZs),
  IGW, **1 NAT** (nat-07e6b6da263ac5ef9, sandbox sizing), 4 route tables,
  7 interface endpoints (ecr.api, ecr.dkr, sts, secretsmanager, kms, logs,
  monitoring — single AZ) + S3 gateway endpoint
- kms: 2 CMKs w/ rotation (data, artifacts), 2 aliases
- s3: rtdp-{artifacts,bundles,checkpoints,validation}-079457921611 —
  versioned, CMK-encrypted, public access blocked
- ecr: 10 repos under rtdp/* — immutable tags, scan-on-push, artifacts CMK
- observability: AMP workspace ws-ebdb423f, SNS alarms topic + email sub,
  CW dashboard, kafka-lag alarm, /rtdp/rtdp-sandbox/services log group

## Verification

`terraform plan` post-apply: **No changes** (zero drift).
Sanity: VPC/NAT/endpoints/buckets/repos confirmed via CLI (all `available`).

## Estimated cost

~$85/mo (1 NAT $32 + 7 endpoints $51 + KMS $2). Under $200 budget cap;
signup credits apply first. Deferred: 4 interface endpoints (kafka, rds,
elasticache, eks) + 2nd/3rd NAT — restore when Stages C/D land.

## Fixes applied during apply

- `ecr` module: `kms_key_arn` var -> repos pinned to artifacts CMK
  (aws/ecr managed key raced repo creation on the fresh account).
- network module right-sized for sandbox (1 NAT, 1-AZ endpoints).
