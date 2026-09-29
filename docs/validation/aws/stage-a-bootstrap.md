# Stage A — bootstrap guardrail verification (PASS)

Date: 2025-09-29. Account: **079457921611** (dedicated sandbox, standalone — org invite pending quota increase). Region: us-east-1.

(Originally verified on 003524518972; rebuilt and re-verified on the dedicated sandbox account the same day.)

## Credential chain (no root for runtime work)

```
aws sso login --profile rtdp-sso        # rtdpuser via IAM Identity Center
  -> AWSReservedSSO_RTDPDevin_8efa12a8befe9cc6   (permission set: sts:AssumeRole -> RTDPDevinRole only)
--profile rtdp-devin                    # chained assume-role
  -> arn:aws:sts::079457921611:assumed-role/RTDPDevinRole/...
```

Permission boundary `RTDPDevinPermissionBoundary` v2 is attached to the role:
Allow-* within guardrails; Deny outside us-east-1 (global services exempt);
Deny IAM users/keys; Deny org/billing mutation; Deny boundary escape/removal.
Root (`aws login` profile `rtdp-new`) was used only for account-level setup:
permission boundary, Devin role/policy, Identity Center assignment, budget.

## terraform plan (bootstrap/, AWS_PROFILE=rtdp-devin)

All five checks evaluated green:

| Check | Result |
|---|---|
| account | 079457921611 |
| region | us-east-1 |
| role_has_boundary | RTDPDevinRole + RTDPDevinPermissionBoundary |
| state_bucket_exists | rtdp-tfstate-079457921611 (versioned, AES256, public-blocked) |
| budget_configured | rtdp-monthly-cap = $200/mo, email alerts 50/80/100% |

Output: `verified = { account, bucket, budget, region, role }`. Zero resources created.

## Fixes applied during setup

- Permission boundary v1 was deny-only (allowed nothing). v2 adds `AllowWithinGuardrails` (Action *) so identity-policy grants intersect correctly; denies unchanged.
- `RTDPDevinPolicy` v2 adds `budgets:List*` — provider's `aws_budgets_budget` data source calls ListTagsForResource.
- Budget creation intentionally stays outside the role (boundary Deny `budgets:ModifyBudget`); created via admin profile.
- Org `o-k5z0u6kfv3` auto-created by Identity Center; `organizations:CreateAccount` hit `ACCOUNT_NUMBER_LIMIT_EXCEEDED` (new-org quota). Decision: run sandbox in this account directly; region lock enforced via boundary instead of SCP.

## Stage A: COMPLETE
