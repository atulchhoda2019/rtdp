#!/usr/bin/env bash
# RTDP sandbox sleep — stop metered compute without destroying state.
# Safe: all data is synthetic and reseedable. Residual burn ~$270/mo
# (MSK + EKS control plane + ElastiCache + Stage B floor).
# Use teardown.sh to go to the ~$85/mo Stage-B-only floor.
#
# Aurora caveat: AWS auto-restarts stopped clusters after 7 days.
# Re-run sleep (or teardown) if the sandbox stays idle that long.
set -euo pipefail
PROFILE="${AWS_PROFILE:-rtdp-devin}"
REGION="${AWS_REGION:-us-east-1}"
CLUSTER=rtdp-sandbox
say() { printf '\033[1m[sleep]\033[0m %s\n' "$*"; }

say "Stopping Managed Flink application (if running)"
aws kinesisanalyticsv2 describe-application --profile "$PROFILE" --region "$REGION" \
  --application-name rtdp-features-sandbox --query 'ApplicationDetail.ApplicationStatus' --output text 2>/dev/null \
  | grep -q RUNNING \
  && aws kinesisanalyticsv2 stop-application --profile "$PROFILE" --region "$REGION" \
       --application-name rtdp-features-sandbox --force >/dev/null \
  && say "flink stopping" || say "flink not running / not present"

say "Scaling EKS node groups to 0"
for ng in system cpu-inference; do
  aws eks update-nodegroup-config --profile "$PROFILE" --region "$REGION" \
    --cluster-name "$CLUSTER" --nodegroup-name "$ng" \
    --scaling-config minSize=0,desiredSize=0 >/dev/null 2>&1 \
    && say "nodegroup $ng -> 0" || say "nodegroup $ng not present"
done

say "Stopping Aurora cluster"
aws rds describe-db-clusters --profile "$PROFILE" --region "$REGION" \
  --db-cluster-identifier "$CLUSTER" --query 'DBClusters[0].Status' --output text 2>/dev/null \
  | grep -qE 'available|stopping' \
  && aws rds stop-db-cluster --profile "$PROFILE" --region "$REGION" \
       --db-cluster-identifier "$CLUSTER" >/dev/null \
  && say "aurora stopping (auto-restarts in 7 days)" || say "aurora not available / not present"

say "Done. Remaining burn: MSK ~\$100, EKS CP ~\$73, ElastiCache ~\$12, Stage B ~\$85"
