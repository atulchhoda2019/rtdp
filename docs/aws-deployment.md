# RTDP on AWS: Deployment Specification for Devin

Companion to `docs/design.md` v2.1 • Draft 1 • September 27, 2026

This document tells Devin how to stand up RTDP in AWS. It does not change the architecture in `design.md`; it maps each component to an AWS service, sets guardrails for working in a real cloud account, and defines the AWS-specific acceptance gates. Where this document and `design.md` disagree on behavior, `design.md` wins. Where they disagree on infrastructure, this document wins.

Everything remains synthetic: tenants, transactions, models, and actions. No real policyholder data, no real enforcement providers, no production account.

---

## 1. Devin prompt

Paste this as the task prompt. Put this file at `docs/aws-deployment.md` first.

> Read `docs/design.md` v2.1, `docs/aws-deployment.md`, and root `AGENTS.md`. Deploy RTDP Phases 0 and 1 to the AWS sandbox account described in `docs/aws-deployment.md`, section 2. Use Terraform for all infrastructure and ArgoCD for all Kubernetes workloads. Follow the service mapping in section 3 exactly; if a mapped service cannot meet a requirement in `design.md` (for example Kafka transactions, Lua atomicity, or checkpoint storage), stop and report it rather than substituting something else. Work in the stage order in section 6. Before every `terraform apply`, post the plan summary and wait for my approval; never run destroy, delete a data store, or change IAM outside the provided permission boundary without explicit approval. Do not create IAM users or long-lived access keys. Keep all data synthetic. Re-run every Phase 1 acceptance gate from `design.md` on AWS, plus the AWS gates in section 7, and save honest evidence (plans, versions, digests, raw results, costs) in `docs/validation/aws/`. Stop and ask before anything touching real customer data, real providers, a non-sandbox account, or a region other than the one specified.

---

## 2. Account guardrails (set up before Devin starts)

These are things **you** provide; Devin should verify them, not create them.

| Guardrail | Setting |
|---|---|
| Account | Dedicated sandbox account in an AWS Organization, not shared with any production workload |
| Region | One region, for example `us-east-1`; deny all others with a Service Control Policy |
| Devin's access | An IAM role Devin assumes through OIDC or short-lived credentials, with a **permission boundary** that blocks IAM user creation, Organizations changes, and billing changes |
| Budget | AWS Budgets alert at 50%, 80% and 100% of a fixed monthly cap, emailed to you |
| Tagging | Every resource tagged `project=rtdp`, `env=sandbox`, `owner=<you>`, `managed_by=terraform`; enforce with a tag policy |
| State | Terraform state in an encrypted, versioned S3 bucket you create, with S3 native state locking |
| Approval | Devin posts `terraform plan` output and waits; you approve each apply |

---

## 3. Service mapping

| Design component | Local (design.md) | AWS target | Notes |
|---|---|---|---|
| Container runtime | Docker Compose | **Amazon EKS**, managed node groups across 3 AZs | Separate node groups for system, decision path, inference, and batch; Karpenter optional |
| Deployment | ArgoCD | **ArgoCD on EKS** | Remains the only workload deployer; CI never applies manifests |
| Kafka-compatible broker | Local broker | **Amazon MSK (provisioned)**, 3 brokers, 3 AZs | Must support idempotent producers, transactions, and `read_committed`; use IAM auth and TLS; confirm transaction support before relying on any serverless option |
| Schema registry | Local contract registry | Keep RTDP's own contract registry; optionally **AWS Glue Schema Registry** for Protobuf transport schemas | Semantic contracts stay in RTDP; Glue validates structure only |
| Flink | JobManager/TaskManager containers | **Amazon Managed Service for Apache Flink**, or the Flink Kubernetes Operator on EKS | Default to the managed service; switch to the operator only if a required connector or setting is unsupported. Checkpoints and savepoints go to S3 |
| Postgres | Local Postgres | **Amazon Aurora PostgreSQL**, Multi-AZ | Row-level security, non-owner runtime role, tenant context set per transaction; RDS Proxy for connection pooling |
| Tier 1 online store | Redis with Lua | **Amazon MemoryDB** (preferred for durability) or **ElastiCache (Valkey/Redis OSS)** for cheaper sandbox runs | Cluster mode: every key for one tenant/entity uses the same hash tag so the Lua script stays single-slot |
| Tier 2 online store | Redis | Same cluster, separate key namespace, or a second ElastiCache cluster | Keep feature-version namespaces separate |
| Object storage | MinIO | **Amazon S3** | Buckets for model artifacts, bundles, Flink checkpoints/savepoints, validation evidence; versioning on; SSE-KMS; block public access |
| Inference | ONNX container | **ONNX service on EKS** (CPU node group) | Keeps the 20 ms budget in-cluster; SageMaker endpoints are a later option only if they meet the p99 budget |
| Secrets | Local files | **AWS Secrets Manager** + External Secrets Operator | Config holds references only |
| Keys | Local | **AWS KMS** customer-managed keys per data class; optional per-tenant keys | Tokenization key material wrapped by KMS, not called per transaction |
| Workload identity | Local fixture | **EKS Pod Identity** (or IRSA) | One IAM role per service; least privilege; model service identities get no write access to activations, flow instances, or action data (ADR-011) |
| Ingress, decision path | Local gRPC | **Network Load Balancer** with TLS, or ALB with gRPC, inside private subnets | mTLS between services; client identity bound to tenant at ingress |
| Ingress, control plane/UI | Local | **ALB + AWS WAF**, authenticated through your IdP (for example Cognito or Okta) | Never on the same listener as the decision path |
| Observability | Prometheus/Grafana/OTel | **ADOT collector → Amazon Managed Service for Prometheus + Amazon Managed Grafana**; CloudWatch Logs; OTel traces | Bounded metric labels, as in design.md |
| Images | Local build | **Amazon ECR** with scan on push | Images signed (cosign) and deployed by digest |
| CI | Local make | **GitHub Actions with OIDC** to a CI role | Builds, tests, signs, pushes to ECR, opens a PR that bumps image digests for ArgoCD |

---

## 4. Network and security baseline

- **VPC:** 3 AZs; private subnets for EKS, MSK, Aurora, MemoryDB/ElastiCache; public subnets only for load balancers and NAT.
- **VPC endpoints:** S3, ECR (api and dkr), STS, Secrets Manager, KMS, CloudWatch Logs, and Managed Prometheus, so traffic stays off the internet.
- **Security groups:** one per component, allowing only the ports in the topic and RPC contracts. No `0.0.0.0/0` inbound except the public load balancer listener, and only if you approve it.
- **Encryption:** TLS in transit everywhere; KMS at rest for Aurora, MSK, MemoryDB/ElastiCache, S3, EBS, and Secrets Manager.
- **MSK access:** IAM authentication with topic-level permissions per service. Tenants never get direct consumer access to shared topics.
- **Aurora:** RLS on every tenant-private table; application connects with a runtime role that cannot bypass RLS; migrations run with a separate role.
- **Audit:** CloudTrail on (organization trail preferred); S3 access logs for artifact buckets.
- **Tenant isolation on AWS:** S3 prefixes per owner scope with IAM conditions; Kafka keys and topics per design.md; per-tenant quotas enforced in the app; per-tenant KMS keys only if you decide the stronger tier is needed.

---

## 5. Repository additions

```text
rtdp/
  infra/
    terraform/
      bootstrap/          # verifies state bucket, permission boundary, budget (does not create them)
      modules/
        network/  eks/  msk/  aurora/  memorydb/  s3/  flink/
        observability/  ecr/  iam-workloads/  kms/
      envs/
        sandbox/          # the only environment for now
  deploy/
    helm/                 # existing charts; add aws values layer
    argocd/               # app-of-apps pointing at deploy/helm
  docs/
    aws-deployment.md     # this file
    validation/aws/       # evidence
```

Pin Terraform and provider versions. Keep modules small, each with a README listing inputs, outputs, and cost drivers.

---

## 6. Stages

Each stage ends with a plan review, apply, and a short evidence note in `docs/validation/aws/`.

| Stage | Scope | Done when |
|---|---|---|
| **A. Verify guardrails** | Confirm region, permission boundary, budget, tag policy, state bucket | Devin reports what it found; nothing created |
| **B. Foundation** | VPC, endpoints, KMS keys, ECR, S3 buckets, CloudTrail check | `terraform plan` clean on re-run; no public buckets |
| **C. Data services** | MSK, Aurora, MemoryDB/ElastiCache, Secrets Manager entries | Connectivity tests from a test pod; Kafka transaction smoke test passes with `read_committed` |
| **D. Compute platform** | EKS, node groups, Pod Identity, External Secrets, ArgoCD, ADOT, Managed Prometheus/Grafana | ArgoCD healthy; a sample app deploys by digest |
| **E. Streaming** | Managed Flink application, S3 checkpoints, MSK connector with transactional sink | Checkpoints succeed at the design.md interval; snapshot and restore works |
| **F. RTDP services** | All Phase 0–1 services through ArgoCD, seeded synthetic tenants, bundles, model artifact | End-to-end synthetic transaction produces a decision and a simulated action |
| **G. Validation** | All Phase 1 gates from design.md plus section 7 gates | Evidence committed, including failures |
| **H. Cost and teardown** | Cost report; scale-to-zero script for off hours; documented teardown | You can stop spend with one command after approval |

---

## 7. AWS-specific acceptance gates

These are in addition to every Phase 1 gate in `design.md`.

| Gate | Pass condition |
|---|---|
| Region lock | Attempting to create a resource in another region fails |
| No static credentials | No IAM users or access keys exist; all workloads use Pod Identity/IRSA; CI uses OIDC |
| Least privilege | Each service can reach only its own topics, buckets, secrets, and tables; model identities cannot write activations, flow instances, or action data |
| Encryption | Every data store and bucket reports KMS encryption; TLS enforced on MSK, Aurora, MemoryDB/ElastiCache |
| AZ failure | Draining one AZ's nodes keeps the decision path serving; report latency impact honestly |
| Broker restart | Rolling an MSK broker does not lose committed decisions or duplicate action effects |
| Flink restore | Stopping and restoring the Flink application from snapshot does not inflate feature totals |
| Tier 1 failover | MemoryDB/ElastiCache primary failover: report counter correctness and any lost updates; apply the design.md fallback when the store is unavailable |
| Aurora failover | Failover completes; action dispatcher and outbox recover without duplicate effects |
| Tenant isolation | Cross-tenant reads fail at the database (RLS), Kafka (IAM), and S3 (IAM) layers, not just in the app |
| Latency on AWS | 500 TPS for 5 minutes; report p50/p95/p99/p99.99 ingress-to-egress, instance types, and AZ placement |
| Cost | Daily cost during the load test and idle, broken down by service |
| Teardown | Documented; dry-run output reviewed before any real teardown |

---

## 8. Defaults Devin should use unless you say otherwise

| Decision | Default |
|---|---|
| Region | `us-east-1` |
| Environments | `sandbox` only |
| IaC | Terraform |
| Flink | Amazon Managed Service for Apache Flink |
| Tier 1 store | ElastiCache (Valkey) for sandbox cost; MemoryDB before any production discussion |
| Inference | ONNX on EKS CPU nodes |
| Instance sizes | Smallest sizes that pass the 500 TPS gate; record them |
| Off-hours | Scale EKS node groups to minimum and stop the Flink app outside test windows |

---

## 9. Decisions for you before Devin starts

- Which AWS account and region, and the monthly budget cap.
- Your identity provider for the control-plane UI.
- Whether sandbox should use MemoryDB now (more durable, higher cost) or ElastiCache.
- Whether Devin may create a public load balancer, or everything stays private behind a VPN or bastion.
- Who approves `terraform apply`, and how fast you can respond, since Devin will wait.

---

## 10. AGENTS.md additions

Add these rules to root `AGENTS.md`:

- Never run `terraform apply` or `destroy` without a posted plan and explicit approval.
- Never create IAM users, access keys, or resources outside the approved region.
- Never use real customer data, real insurer credentials, or real action providers.
- Never grant a model service identity write, activation, or dispatch permissions (ADR-011).
- Record every AWS resource change and its cost impact in `docs/validation/aws/`.
- If an AWS managed service cannot meet a design.md guarantee, stop and report; do not quietly swap components or weaken a test.
