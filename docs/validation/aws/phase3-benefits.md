# Phase 3 — benefits products on AWS (rtdp-sandbox, us-east-1)

BUC-1..BUC-5 from `docs/demo-benefits.md` deployed and verified on the
live sandbox — entirely configuration: new contracts, ONNX/SLM artifacts,
bindings, rulesets, action policies, products, tenant overlays, seed
wiring. No service code changed (`git diff` confirms zero changes under
`services/`, `internal/`, `cmd/`, `proto/`).

## Deployment steps that ran

1. `docker buildx --platform linux/amd64` tools image → ECR
   `rtdp/tools@sha256:4f2d8262…` (includes `ml/slm-dependent-verification`
   and retrained models: hsa champion `sha256:50dbd446…`, challenger
   `sha256:092955d0…` — AMD64 digests differ from local ARM64 builds,
   each environment pins its own).
2. `toolsImage` digest restamped in `deploy/argocd/values/sandbox.yaml`.
3. `kubectl -n rtdp exec deploy/ollama -- ollama pull qwen2.5-coder:0.5b`
   (running pod; the init-container pull list in
   `deploy/argocd/apps/ollama.yaml` covers fresh pods).
4. ArgoCD `rtdp-bootstrap` sync → PostSync `rtdp-seed` job: 5 ONNX models
   uploaded, 2 SLM artifacts pinned (narrative `c5396e06…`, dependent
   `20693aeb…` — distinct weights required because the slm-service
   registry keys by weights digest), 16 bundles compiled+synced
   (7 products × 2 tenants + `hsa_reimbursement@2` challenger under
   `cohort: shadow` for tenant_a), all warms green.
5. `rollout restart deployment/orchestrator` — activations sync from S3
   only at pod start (initContainer `aws s3 sync`), so the new manifest
   required a restart; this is the designed pin point, not a live read.

## Evidence (run 2026-09-30, ingress via `kubectl port-forward svc/ingress 8080`)

`tests/e2e/test_benefits_use_cases.py`: **13 pass, 0 fail, 2 env-skips**
(the two skips need `RTDP_PSQL` — Aurora isn't reachable without an
in-cluster psql client; both already pass on the local stack, which runs
the same code):

- `DEPENDENT_VERIFICATION` → `DECISION_APPROVE / DOCUMENT_CONSISTENT`
  (in-cluster SLM `slm_dependent_verification` @ `qwen2.5-coder:0.5b`).
- `HSA_CLAIM` $320 → Client A `APPROVE / AUTO_ADJUDICATED`
  (bundle `sha256:df4334f3…`), Client B `REVIEW / OVER_AUTO_APPROVE_LIMIT`
  (bundle `sha256:24b32706…`) — same model, per-tenant pinned bundles.
- `CONTRIBUTION_CHANGE` → small change `APPROVE` + intent;
  first-time large change `REVIEW / ACCOUNT_TAKEOVER_PATTERN`.
- Champion live digest ≠ shadow activation digest
  (`sha256:86b01002…` on AWS).
- Regression: `tests/e2e/test_use_cases.py` **11/11** on AWS.
- Seed job `rtdp-seed` Completed (all 7 event types × 2 tenants warm);
  ArgoCD 14/14 apps Synced/Healthy.

## Notes

- `BUC-3` live-change evidence (overlay 250→400→250, identical image
  digests + migration set) is in `docs/validation/phase3-config-only.json`,
  produced on the local stack; the same `compile → activate` path applies
  on AWS via bundle sync + orchestrator restart.
- Runtime-surface limits (empty rules `Input`, no cohort routing, no
  automated reconciler): `docs/validation/phase3-gaps.md`.
