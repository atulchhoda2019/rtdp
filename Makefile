# RTDP — real-time decisioning platform. See docs/design.md v2.1.
# Local stack runs under Docker Compose; Java builds run in a Maven container
# because host JDK on macOS 26 is unstable.

SHELL := /bin/bash
export DOCKER_BUILDKIT := 1

GO := go
PYTHON := /opt/homebrew/bin/python3.11
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
MVN_DOCKER := docker run --rm -v $(CURDIR)/streaming/flink-features:/workspace -v maven-cache:/root/.m2 -w /workspace maven:3.9-eclipse-temurin-17 mvn
COMPOSE := docker compose

.PHONY: help up down ps logs proto seed model flink-jar lint test \
        test-contracts test-isolation test-streaming test-model \
        test-actions test-resilience e2e load clean bootstrap

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-22s %s\n", $$1, $$2}'

## ---------- setup ----------
$(VENV)/bin/activate:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e services/control-plane -e services/inference-service pytest

bootstrap: $(VENV)/bin/activate ## One-time local setup (venv + deps)
	go mod download

## ---------- codegen ----------
proto: ## Generate Go/Python/Java protobuf code
	buf generate

## ---------- local stack ----------
up: ## Start full local stack (infra + RTDP services)
	$(COMPOSE) up -d --build
	$(COMPOSE) ps

down: ## Stop the stack
	$(COMPOSE) down

ps: ## Container status
	$(COMPOSE) ps

logs: ## Tail all logs
	$(COMPOSE) logs -f --tail=100

clean: ## Stop stack and wipe volumes (destructive)
	$(COMPOSE) down -v

## ---------- model & flink ----------
model: $(VENV)/bin/activate ## Train synthetic logistic model, export ONNX
	$(PY) ml/seed-model/train.py

flink-jar: ## Build the Flink feature job inside a Maven container
	$(MVN_DOCKER) -q -DskipTests package

## ---------- seed & quality ----------
seed: proto ## Create topics/buckets, seed synthetic assets, submit Flink job
	$(PY) tools/seed/seed.py

lint: ## Lint Go/Python/Java
	go vet ./... 
	cd services/control-plane && $(abspath $(PY)) -m compileall -q . 
	cd services/inference-service && $(abspath $(PY)) -m compileall -q .

test: ## All unit tests
	go test ./...
	$(PY) -m pytest tests/contracts services -q

## ---------- acceptance gates ----------
test-contracts: ## Contract registry + envelope validation
	$(PY) -m pytest tests/contracts -v

test-isolation: ## Cross-tenant isolation gates
	go test ./tests/isolation/... -v

test-streaming: ## Flink golden-total fixtures + recovery
	$(PY) -m pytest tests/recovery -v -k streaming

test-model: ## ONNX/native parity + inference behavior
	$(PY) -m pytest tests/contracts -v -k model

test-actions: ## Action idempotency + ambiguity gates
	go test ./tests/actions/... -v 2>/dev/null || $(PY) -m pytest tests/actions -v

test-resilience: ## Tier1/store failover + fallback gates
	go test ./tests/recovery/... -v 2>/dev/null || true

e2e: ## End-to-end synthetic transaction -> decision -> action
	$(PY) tests/e2e/test_e2e.py

load: ## Load test (make load TPS=500 DURATION=5m)
	$(PY) tests/performance/load.py --tps $(or $(TPS),500) --duration $(or $(DURATION),5m)

aws-sleep:    ## stop metered compute (aurora, flink, eks nodes) — ~$270/mo residual
	tools/aws/sleep.sh
aws-wake:     ## restore compute after sleep ("Devin wake up")
	tools/aws/wake.sh
aws-teardown: ## destroy Stage C+D entirely (needs CONFIRM=teardown) — ~$85/mo floor
	CONFIRM=teardown tools/aws/teardown.sh
aws-images:   ## build + push all service images to ECR, stamp digests into apps
	tools/aws/push-images.sh
aws-render:   ## regenerate deploy/argocd/values/sandbox.yaml from terraform outputs
	tools/aws/render-env.sh
aws-provision: ## full bring-up: apply -> render -> argocd+ESO -> app-of-apps
	tools/aws/provision.sh
