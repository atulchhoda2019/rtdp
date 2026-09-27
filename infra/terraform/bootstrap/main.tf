# Stage A bootstrap: VERIFY guardrails exist — this module creates nothing.
# It confirms the account guardrails from docs/aws-deployment.md section 2
# before any real stage may plan/apply.

terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
}

variable "expected_account_id" {
  type = string
}
variable "expected_region" {
  type    = string
  default = "us-east-1"
}
variable "state_bucket" {
  type = string
}
variable "devin_role_arn" {
  type = string
}
variable "permission_boundary_arn" {
  type = string
}
variable "monthly_budget_name" {
  type    = string
  default = "rtdp-monthly-cap"
}
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_iam_role" "devin" {
  name = split("/", var.devin_role_arn)[1]
}
data "aws_s3_bucket" "state" {
  bucket = var.state_bucket
}
data "aws_budgets_budget" "cap" {
  name = var.monthly_budget_name
}
# --- checks: fail loudly rather than create anything ---
check "account" {
  assert {
    condition     = data.aws_caller_identity.current.account_id == var.expected_account_id
    error_message = "Wrong account: got ${data.aws_caller_identity.current.account_id}, expected ${var.expected_account_id}."
  }
}
check "region" {
  assert {
    condition     = data.aws_region.current.name == var.expected_region
    error_message = "Wrong region: got ${data.aws_region.current.name}, expected ${var.expected_region}."
  }
}
check "role_has_boundary" {
  assert {
    condition     = data.aws_iam_role.devin.permissions_boundary == var.permission_boundary_arn
    error_message = "Devin role is missing the required permission boundary."
  }
}
check "state_bucket_exists" {
  assert {
    condition     = data.aws_s3_bucket.state.bucket == var.state_bucket
    error_message = "State bucket not found."
  }
}
check "budget_configured" {
  assert {
    condition     = data.aws_budgets_budget.cap.name == var.monthly_budget_name
    error_message = "Monthly budget cap not found."
  }
}

output "verified" {
  value = {
    account = data.aws_caller_identity.current.account_id
    region  = data.aws_region.current.name
    role    = data.aws_iam_role.devin.arn
    bucket  = data.aws_s3_bucket.state.bucket
    budget  = data.aws_budgets_budget.cap.name
  }
}
