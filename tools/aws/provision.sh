#!/usr/bin/env bash
# RTDP sandbox provision ("Devin provision"):
#   1. terraform apply  (prompts; AGENTS.md requires explicit human approval)
#   2. render env values + commit/push so ArgoCD sees them
#   3. kubeconfig + install ArgoCD + External Secrets Operator (pinned charts)
#   4. apply AppProject + root app-of-apps -> syncs services + PostSync seed
#
# Idempotent; safe to re-run. Sleep/wake cover cheaper pauses.
set -euo pipefail
PROFILE="${AWS_PROFILE:-rtdp-devin}"
REGION="${AWS_REGION:-us-east-1}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export AWS_PROFILE="$PROFILE" AWS_REGION="$REGION"

ARGOCD_CHART_VERSION="10.9.3"   # app v3.5.3
ESO_CHART_VERSION="2.11.0"      # app v2.11.0

cd "$ROOT/infra/terraform/envs/sandbox"
echo "[provision] terraform apply (interactive — review the plan)"
terraform apply

echo "[provision] rendering env values"
"$ROOT/tools/aws/render-env.sh"

cd "$ROOT"
if ! git diff --quiet -- deploy/argocd/values/sandbox.yaml; then
  git add deploy/argocd/values/sandbox.yaml
  git commit -m "env: render sandbox endpoints" >/dev/null
  git push
fi

CLUSTER=$(terraform -chdir=infra/terraform/envs/sandbox output -raw eks_cluster_name)
aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" --profile "$PROFILE"

helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
helm repo add external-secrets https://charts.external-secrets.io >/dev/null 2>&1 || true
helm repo update >/dev/null

helm upgrade --install argocd argo/argo-cd \
  --namespace argocd --create-namespace \
  --version "$ARGOCD_CHART_VERSION" --wait --timeout 10m

helm upgrade --install external-secrets external-secrets/external-secrets \
  --namespace external-secrets --create-namespace \
  --version "$ESO_CHART_VERSION" --wait --timeout 5m

kubectl apply -f deploy/argocd/project.yaml
kubectl apply -f deploy/argocd/root-app.yaml

echo "[provision] done — watch: kubectl -n argocd get applications"
echo "[provision] demo:   kubectl -n rtdp port-forward svc/ingress 8080"
