#!/usr/bin/env bash
# Build + push every RTDP service image (and the tools image) to ECR, then
# stamp real sha256 digests into the ArgoCD app manifests and values file.
# Requires: docker (colima), AWS_PROFILE with ecr push perms (rtdp-devin).
set -euo pipefail
PROFILE="${AWS_PROFILE:-rtdp-devin}"
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT=079457921611
REGISTRY="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

aws ecr get-login-password --profile "$PROFILE" --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null

TAG="git-$(git rev-parse --short HEAD)"

# name -> dockerfile (bash 3.2 compatible: no assoc arrays)
SERVICES="ingress orchestrator feature-service signal-resolver rules-service \
inference-service slm-service feature-materializer action-dispatcher projector \
agent-registry approval-service agent-gateway tools"

dockerfile_for() {
  case "$1" in
    tools) echo "tools/seed/Dockerfile" ;;
    *)     echo "services/$1/Dockerfile" ;;
  esac
}

for s in $SERVICES; do
  img="$REGISTRY/rtdp/$s"
  echo "[images] build+push $s:$TAG"
  docker buildx build --platform linux/amd64 --push \
    -f "$(dockerfile_for "$s")" -t "$img:$TAG" . >/dev/null
  digest=$(aws ecr describe-images --profile "$PROFILE" --region "$REGION" \
    --repository-name "rtdp/$s" --image-ids imageTag="$TAG" \
    --query 'imageDetails[0].imageDigest' --output text)
  echo "[images]   -> $digest"
  if [ "$s" = "tools" ]; then
    sed -i '' "s|toolsImage: \".*\"|toolsImage: \"$img@$digest\"|" \
      deploy/argocd/values/sandbox.yaml
  else
    sed -i '' "s|digest: \"sha256:.*\"|digest: \"$digest\"|" "deploy/argocd/apps/$s.yaml"
  fi
done

# Ollama: pin the upstream image by digest (no copy to ECR needed).
docker pull ollama/ollama:0.12.6 >/dev/null
DIG=$(docker inspect ollama/ollama:0.12.6 \
  --format '{{index .RepoDigests 0}}' | sed 's/.*@//')
sed -i '' "s|ollama/ollama@sha256:SET_BY_PUSH_IMAGES|ollama/ollama@$DIG|; s|repository: ollama/ollama$|repository: ollama/ollama|" deploy/argocd/apps/ollama.yaml
sed -i '' "s|digest: \"sha256:SET_BY_PUSH_IMAGES\"|digest: \"$DIG\"|" deploy/argocd/apps/ollama.yaml
echo "[images] ollama -> $DIG"
echo "[images] done — commit the stamped manifests"
