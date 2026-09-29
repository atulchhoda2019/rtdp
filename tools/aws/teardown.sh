#!/usr/bin/env bash
# RTDP sandbox teardown — DESTROYS Stage C+D resources (MSK, Aurora,
# ElastiCache, EKS, Flink, workload roles). Stage B foundation survives
# (~$85/mo floor: VPC, NAT, endpoints, S3, ECR, KMS).
#
# All state is synthetic and reseedable — Aurora/MSK contents are rebuilt
# by tools/seed + terraform apply. Still: this runs terraform destroy, so
# it requires CONFIRM=teardown and is only run on explicit user instruction.
set -euo pipefail
[ "${CONFIRM:-}" = "teardown" ] || { echo "refusing: set CONFIRM=teardown"; exit 1; }
PROFILE="${AWS_PROFILE:-rtdp-devin}"
cd "$(dirname "$0")/../../infra/terraform/envs/sandbox"

echo "[teardown] destroying Stage C+D modules (keeps network/kms/s3/ecr/observability)"
AWS_PROFILE="$PROFILE" terraform destroy -auto-approve \
  -target=module.msk \
  -target=module.aurora \
  -target=module.elasticache \
  -target=module.eks \
  -target=module.iam_workloads \
  -target=module.flink \
  -target='module.network.aws_vpc_endpoint.interface["kafka"]' \
  -target='module.network.aws_vpc_endpoint.interface["rds"]' \
  -target='module.network.aws_vpc_endpoint.interface["elasticache"]' \
  -target='module.network.aws_vpc_endpoint.interface["eks"]'

echo "[teardown] done — Stage B floor only (~\$85/mo). Wake via terraform apply."
