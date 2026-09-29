# RTDP AWS guardrail setup (one-time, human steps)

Everything in `docs/aws-deployment.md` section 2 must exist **before** Devin deploys.
These steps need AWS Organizations management-account access or an administrator in
the sandbox account — Devin cannot create them without defeating the point of the
guardrails.

**Current status:** all Terraform modules (Stages A–D) exist and pass
`terraform validate`. Nothing has been applied — no AWS credentials are
configured on the working machine yet. Every item below is still pending.

When finished, hand Devin: **account ID, region, Devin role ARN, state bucket name**,
and the monthly budget cap. The mechanical handoff is a filled-in
`infra/terraform/bootstrap/terraform.tfvars` (template:
`terraform.tfvars.example`, real file gitignored) plus working credentials
(`aws sts get-caller-identity` must return the sandbox account).

## 1. Sandbox account

Create a dedicated account in your AWS Organization (e.g. `rtdp-sandbox`). Do not
share it with any production workload.

## 2. Region lock (SCP)

Attach this Service Control Policy to the sandbox account (replace `us-east-1` if
you choose a different region). It denies all API calls outside the approved region
except global services that cannot be region-scoped.

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "DenyOutsideApprovedRegion",
    "Effect": "Deny",
    "NotAction": [
      "iam:*", "organizations:*", "sts:*", "cloudfront:*", "route53:*",
      "budgets:*", "ce:*", "support:*", "globalaccelerator:*", "account:*"
    ],
    "Resource": "*",
    "Condition": {"StringNotEquals": {"aws:RequestedRegion": "us-east-1"}}
  }]
}
```

## 3. Permission boundary

Create this customer-managed policy in the sandbox account as
`RTDPDevinPermissionBoundary`. It caps what any role Devin creates can ever do —
no IAM users, no long-lived keys, no Organizations/billing changes.

```json
{
  "Version": "2012-10-17",
  "Statement": [
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
      "Action": ["organizations:*", "account:*", "budgets:*", "ce:*", "cur:*", "aws-portal:*"],
      "Resource": "*"
    },
    {
      "Sid": "DenyBoundaryEscape",
      "Effect": "Deny",
      "Action": [
        "iam:DeleteRolePermissionsBoundary",
        "iam:DeleteUserPermissionsBoundary",
        "iam:PutRolePermissionsBoundary"
      ],
      "Resource": "*",
      "Condition": {"StringNotEquals": {"iam:PermissionsBoundary":
        "arn:aws:iam::<ACCOUNT_ID>:policy/RTDPDevinPermissionBoundary"}}
    },
    {
      "Sid": "DenyRemoveThisBoundary",
      "Effect": "Deny",
      "Action": ["iam:DeletePolicyVersion", "iam:DeletePolicy", "iam:SetDefaultPolicyVersion"],
      "Resource": "arn:aws:iam::<ACCOUNT_ID>:policy/RTDPDevinPermissionBoundary"
    }
  ]
}
```

Verify:

```bash
aws iam get-policy \
  --policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/RTDPDevinPermissionBoundary
```

## 4. Devin's role

Create a role `RTDPDevinRole` in the sandbox account with the permission boundary
attached. Trust policy depends on how Devin authenticates:

- **Recommended — OIDC from CI (GitHub Actions):** trust the GitHub OIDC provider
  (`token.actions.githubusercontent.com`), audience `sts.amazonaws.com`, restricted
  to your repository. No stored keys.
- **Local development:** allow your own admin principal to assume the role, then
  give Devin `aws sts assume-role` credentials for that principal, or issue
  short-lived credentials per session. Do not create an IAM user for Devin.

On the working machine, Devin needs any credential chain that resolves —
in preference order:

```bash
aws login                          # AWS CLI console-credentials flow (new CLI)
aws sso login --profile <profile>  # IAM Identity Center / SSO
# or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN env vars
```

Verify before handoff:

```bash
aws sts get-caller-identity   # must return the SANDBOX account id
aws sts assume-role \
  --role-arn arn:aws:iam::<ACCOUNT_ID>:role/RTDPDevinRole \
  --role-session-name guardrail-check   # confirms the trust policy works
```

Attach a policy granting the services RTDP needs (EC2/VPC, EKS, MSK, RDS/Aurora,
ElastiCache/MemoryDB, S3, KMS, Secrets Manager, ECR, IAM roles/policies *with the
boundary required*, Flink, AMP/AMG, CloudWatch, CloudTrail read, ELB, WAF, pricing
read). Force the boundary on role creation:

```json
{
  "Sid": "RolesMustCarryBoundary",
  "Effect": "Deny",
  "Action": ["iam:CreateRole", "iam:PutRolePolicy", "iam:AttachRolePolicy"],
  "Resource": "*",
  "Condition": {"StringNotEquals": {"iam:PermissionsBoundary":
    "arn:aws:iam::<ACCOUNT_ID>:policy/RTDPDevinPermissionBoundary"}}
}
```

## 5. Terraform state bucket

```bash
aws s3api create-bucket --bucket rtdp-tfstate-<ACCOUNT_ID> --region us-east-1
aws s3api put-bucket-versioning --bucket rtdp-tfstate-<ACCOUNT_ID> \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket rtdp-tfstate-<ACCOUNT_ID> \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms"}}]}'
aws s3api put-public-access-block --bucket rtdp-tfstate-<ACCOUNT_ID> \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

S3 native state locking (`use_lockfile = true`) is used — no DynamoDB table needed.

Verify:

```bash
aws s3api head-bucket --bucket rtdp-tfstate-<ACCOUNT_ID>
aws s3api get-bucket-versioning --bucket rtdp-tfstate-<ACCOUNT_ID>   # Enabled
aws s3api get-bucket-encryption --bucket rtdp-tfstate-<ACCOUNT_ID>   # aws:kms
aws s3api get-public-access-block --bucket rtdp-tfstate-<ACCOUNT_ID> # all true
```

## 6. Budget

Create an AWS Budget (`rtdp-monthly-cap`, cost budget, your monthly cap) with email
notifications at 50%, 80%, and 100% of actual spend.

Verify:

```bash
aws budgets describe-budgets --account-id <ACCOUNT_ID> | grep rtdp-monthly-cap
```

## 7. Tag policy (optional but recommended)

Organization tag policy requiring on every taggable resource:
`project=rtdp`, `env=sandbox`, `owner=<you>`, `managed_by=terraform`.

## 8. Approvals

Devin posts `terraform plan` output and waits. Decide who approves applies and how.
Tell Devin the approval channel before Stage B begins.

## 9. Hand Devin the values

Copy the template and fill it in — this file is gitignored, so account-specific
values stay local:

```bash
cp infra/terraform/bootstrap/terraform.tfvars.example \
   infra/terraform/bootstrap/terraform.tfvars
```

Then Devin runs Stage A:

```bash
cd infra/terraform/bootstrap && terraform init && terraform plan
```

The five `check` blocks evaluate during plan — a missing boundary, bucket, or
budget fails loudly with a named error. Stage A creates nothing and has no
state backend; it is verification only.

## Handoff checklist

| Item | tfvars key | Value | Status |
|---|---|---|---|
| Account ID | `expected_account_id` | | ☐ pending |
| Region | `expected_region` | `us-east-1` (or chosen) | ☐ pending |
| Devin role ARN | `devin_role_arn` | | ☐ pending |
| State bucket | `state_bucket` | `rtdp-tfstate-<ACCOUNT_ID>` | ☐ pending |
| Permission boundary ARN | `permission_boundary_arn` | `arn:aws:iam::<ACCOUNT_ID>:policy/RTDPDevinPermissionBoundary` | ☐ pending |
| Monthly budget cap | `monthly_budget_name` | `rtdp-monthly-cap` | ☐ pending |
| Credentials working | — (`aws sts get-caller-identity`) | | ☐ pending |
| Approval contact | — | | ☐ pending |
| IdP for control-plane UI | — (Stage D input) | | ☐ pending |
| Public LB allowed? | — (Stage D input) | | ☐ pending |
