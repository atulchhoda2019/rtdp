#!/usr/bin/env bash
# RTDP sandbox wake — reverse of sleep.sh. "Devin provision" / "Devin wake up".
# Restores Aurora, EKS node groups, and starts the Flink application.
# If modules were torn down (teardown.sh), this detects that and tells you
# to run terraform apply instead.
set -euo pipefail
PROFILE="${AWS_PROFILE:-rtdp-devin}"
REGION="${AWS_REGION:-us-east-1}"
CLUSTER=rtdp-sandbox
say() { printf '\033[1m[wake]\033[0m %s\n' "$*"; }

# If core resources are gone (teardown), wake can't fix it — needs terraform.
if ! aws eks describe-cluster --profile "$PROFILE" --region "$REGION" --name "$CLUSTER" \
     --query 'cluster.status' --output text 2>/dev/null | grep -q .; then
  say "EKS cluster missing — sandbox was TORN DOWN."
  say "Restore with: cd infra/terraform/envs/sandbox && AWS_PROFILE=$PROFILE terraform apply"
  exit 1
fi

say "Starting Aurora cluster"
aws rds start-db-cluster --profile "$PROFILE" --region "$REGION" \
  --db-cluster-identifier "$CLUSTER" >/dev/null 2>&1 && say "aurora starting" || say "aurora already up or absent"

say "Restoring EKS node groups"
for ng in system cpu-inference; do
  aws eks update-nodegroup-config --profile "$PROFILE" --region "$REGION" \
    --cluster-name "$CLUSTER" --nodegroup-name "$ng" \
    --scaling-config minSize=1,desiredSize=1 >/dev/null 2>&1 \
    && say "nodegroup $ng -> 1" || say "nodegroup $ng not present"
done

say "Starting Managed Flink application"
aws kinesisanalyticsv2 start-application --profile "$PROFILE" --region "$REGION" \
  --application-name rtdp-features-sandbox --run-configuration '{"ApplicationRestoreConfiguration":{"ApplicationRestoreType":"RESTORE_FROM_LATEST_SNAPSHOT"}}' \
  >/dev/null 2>&1 && say "flink starting" || say "flink absent or already running"

say "Wake initiated — services take ~5-10 min to be ready."
say "kubectl access: aws eks update-kubeconfig --profile $PROFILE --name $CLUSTER --region $REGION"
