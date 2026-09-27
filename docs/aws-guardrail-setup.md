# RTDP AWS guardrail setup (one-time, human steps)

Everything in `docs/aws-deployment.md` section 2 must exist **before** Devin deploys.
These steps need AWS Organizations management-account access or an administrator in
the sandbox account — Devin cannot create them without defeating the point of the
guardrails.

When finished, hand Devin: **account ID, region, Devin role ARN, state bucket name**,
and the monthly budget cap.

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

## 4. Devin's role

Create a role `RTDPDevinRole` in the sandbox account with the permission boundary
attached. Trust policy depends on how Devin authenticates:

- **Recommended — OIDC from CI (GitHub Actions):** trust the GitHub OIDC provider
  (`token.actions.githubusercontent.com`), audience `sts.amazonaws.com`, restricted
  to your repository. No stored keys.
- **Local development:** allow your own admin principal to assume the role, then
  give Devin `aws sts assume-role` credentials for that principal, or issue
  short-lived credentials per session. Do not create an IAM user for Devin.

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

## 6. Budget

Create an AWS Budget (`rtdp-monthly-cap`, cost budget, your monthly cap) with email
notifications at 50%, 80%, and 100% of actual spend.

## 7. Tag policy (optional but recommended)

Organization tag policy requiring on every taggable resource:
`project=rtdp`, `env=sandbox`, `owner=<you>`, `managed_by=terraform`.

## 8. Approvals

Devin posts `terraform plan` output and waits. Decide who approves applies and how.
Tell Devin the approval channel before Stage B begins.

## Handoff checklist

| Item | Value |
|---|---|
| Account ID | |
| Region | `us-east-1` (or chosen) |
| Devin role ARN | |
| State bucket | `rtdp-tfstate-<ACCOUNT_ID>` |
| Permission boundary ARN | `arn:aws:iam::<ACCOUNT_ID>:policy/RTDPDevinPermissionBoundary` |
| Monthly budget cap | |
| Approval contact | |
| IdP for control-plane UI | |
| Public LB allowed? | |
