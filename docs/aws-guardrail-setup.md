# RTDP AWS guardrail setup

Everything in `docs/aws-deployment.md` section 2 must exist **before** Devin deploys.
Account-level pieces need administrator credentials — Devin cannot create them
without defeating the point of the guardrails.

**Current status (2025-09-29): Stage A COMPLETE — all five bootstrap checks pass.**
Account `079457921611`, region `us-east-1`. Zero infrastructure applied. Evidence:
`docs/validation/aws/stage-a-bootstrap.md`.

Dedicated sandbox account `079457921611` (new AWS account, Paid plan, ~$200
signup credits). Standalone for now — org `o-k5z0u6kfv3` membership is blocked
by the new-org account quota (increase request pending, case filed
2025-09-29). Region lock is enforced by the permission boundary; the SCP layer
can be added if/when the account joins the org. Management account
`003524518972` now holds only billing/org admin.

## As-built topology

```
aws sso login --profile rtdp-sso        # rtdpuser @ IAM Identity Center
  -> AWSReservedSSO_RTDPDevin_*         # permission set: ONLY sts:AssumeRole -> RTDPDevinRole
--profile rtdp-devin                    # chained assume-role (1 h sessions)
  -> RTDPDevinRole                      # RTDPDevinPolicy + RTDPDevinPermissionBoundary
```

- `rtdp-new` profile (`aws login`, root-backed, sandbox acct) —
  **bootstrap/admin only**. Used for: boundary, role, policy, Identity Center,
  budget.
- `rtdp` profile (`aws login`, root-backed, mgmt acct `003524518972`) —
  org/billing admin only.
- `rtdp-sso` profile — Identity Center user, powers limited to assuming the role.
- `rtdp-devin` profile — the actual working credential. All Terraform and
  deployment operations run under this.

## 1. Account

Standalone account `079457921611` (Organization `o-k5z0u6kfv3` auto-created by
Identity Center; member-account creation blocked by new-org quota — revisit if
a dedicated member account is wanted later).

## 2. Region lock

Implemented inside the permission boundary (see §3): `DenyOutsideApprovedRegion`
denies non-global API calls where `aws:RequestedRegion != us-east-1`.

## 3. Permission boundary (DEPLOYED — v2)

`arn:aws:iam::079457921611:policy/RTDPDevinPermissionBoundary`. Caps what any
role Devin creates can ever do. The `AllowWithinGuardrails` statement is
required — a deny-only boundary intersects to *nothing*.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowWithinGuardrails",
      "Effect": "Allow",
      "Action": "*",
      "Resource": "*"
    },
    {
      "Sid": "DenyOutsideApprovedRegion",
      "Effect": "Deny",
      "NotAction": [
        "iam:*", "sts:*", "cloudfront:*", "route53:*", "budgets:*", "ce:*",
        "cur:*", "support:*", "globalaccelerator:*", "account:*",
        "organizations:*", "pricing:*", "aws-portal:*"
      ],
      "Resource": "*",
      "Condition": {"StringNotEquals": {"aws:RequestedRegion": "us-east-1"}}
    },
    {
      "Sid": "DenyIAMUsersAndKeys",
      "Effect": "Deny",
      "Action": [
        "iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile",
        "iam:UpdateLoginProfile", "iam:AttachUserPolicy", "iam:PutUserPolicy",
        "iam:AddUserToGroup", "iam:CreateServiceLinkedRole"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DenyOrgAndBilling",
      "Effect": "Deny",
      "Action": ["organizations:*", "account:*", "budgets:ModifyBudget",
        "budgets:DeleteBudget", "aws-portal:*"],
      "Resource": "*"
    },
    {
      "Sid": "DenyBoundaryEscape",
      "Effect": "Deny",
      "Action": ["iam:DeleteRolePermissionsBoundary",
        "iam:DeleteUserPermissionsBoundary", "iam:PutRolePermissionsBoundary"],
      "Resource": "*",
      "Condition": {"StringNotEquals": {"iam:PermissionsBoundary":
        "arn:aws:iam::079457921611:policy/RTDPDevinPermissionBoundary"}}
    },
    {
      "Sid": "DenyRemoveThisBoundary",
      "Effect": "Deny",
      "Action": ["iam:DeletePolicyVersion", "iam:DeletePolicy",
        "iam:SetDefaultPolicyVersion"],
      "Resource": "arn:aws:iam::079457921611:policy/RTDPDevinPermissionBoundary"
    }
  ]
}
```

Note: service-linked roles for eks/msk/rds/elasticache/memorydb/eks-fargate were
pre-created before the boundary denied `iam:CreateServiceLinkedRole`.

## 4. Devin's role (DEPLOYED)

`arn:aws:iam::079457921611:role/RTDPDevinRole` — boundary attached, policy
`RTDPDevinPolicy` v2 (RTDP service scope: ec2/eks/kafka/rds/elasticache/memorydb/
s3/kms/secretsmanager/ecr/flink/aps/cloudwatch/logs/elb/wafv2/sns/acm/
autoscaling/tag/ssm-read/ce-read/budgets-read/iam-role-mgmt; `iam:PassRole`
scoped to `rtdp-*` roles; `RolesMustCarryBoundary` forces the boundary on any
role creation).

Authentication: IAM Identity Center user `rtdpuser` (store `d-9a675f84e3`,
portal `https://d-9a675f84e3.awsapps.com/start`) holds permission set
`RTDPDevin` — **only** `sts:AssumeRole` on `RTDPDevinRole`, nothing else.

```bash
aws sso login --profile rtdp-sso                     # browser auth as rtdpuser
aws sts get-caller-identity --profile rtdp-devin     # assumed-role/RTDPDevinRole/...
```

## 5. Terraform state bucket (DEPLOYED)

`rtdp-tfstate-079457921611` — versioning enabled, AES256 SSE, all public access
blocked. S3 native state locking (`use_lockfile = true`) — no DynamoDB needed.

## 6. Budget (DEPLOYED)

`rtdp-monthly-cap` — cost budget, **$200/month**, email alerts at 50/80/100%
ACTUAL to the account owner. Deliberately created via admin credentials — the
boundary denies `budgets:ModifyBudget`/`DeleteBudget`, so Devin can read the
budget but never raise it.

## 7. Tag policy (optional — not done)

Requires Organizations management-account features; deferred. Enforced instead
by Terraform `default_tags` on providers.

## 8. Approvals

Devin posts `terraform plan` output and waits for explicit approval before any
`apply`. Approval channel: this conversation.

## 9. Stage A verification — PASSED

`infra/terraform/bootstrap/terraform.tfvars` (gitignored) is filled with the
real values. `terraform plan` evaluated all five checks green; the `verified`
output echoes account/bucket/budget/region/role. Stage A creates nothing and
has no state backend.

## Handoff checklist

| Item | tfvars key | Value | Status |
|---|---|---|---|
| Account ID | `expected_account_id` | `079457921611` | ✅ |
| Region | `expected_region` | `us-east-1` | ✅ |
| Devin role ARN | `devin_role_arn` | `arn:aws:iam::079457921611:role/RTDPDevinRole` | ✅ |
| State bucket | `state_bucket` | `rtdp-tfstate-079457921611` | ✅ |
| Permission boundary ARN | `permission_boundary_arn` | `arn:aws:iam::079457921611:policy/RTDPDevinPermissionBoundary` | ✅ |
| Monthly budget cap | `monthly_budget_name` | `rtdp-monthly-cap` ($200) | ✅ |
| Credentials working | — | `rtdp-devin` profile (SSO → role chain) | ✅ |
| Approval contact | — | this conversation | ✅ |
| IdP for control-plane UI | — (Stage D input) | Identity Center candidate | ☐ pending |
| Public LB allowed? | — (Stage D input) | | ☐ pending |
| ElastiCache vs MemoryDB | — (Stage C input) | ElastiCache recommended | ☐ pending |
