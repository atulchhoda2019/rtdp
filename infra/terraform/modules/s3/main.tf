# S3 buckets: model artifacts, bundles, Flink checkpoints/savepoints,
# validation evidence. Versioning + SSE-KMS + public access blocked.
variable "tags" {
  type = map(string)
}
variable "kms_key" {
  type = string
}
variable "account_id" {
  type = string
}
locals {
  buckets = {
    artifacts   = "rtdp-artifacts-${var.account_id}"
    bundles     = "rtdp-bundles-${var.account_id}"
    checkpoints = "rtdp-checkpoints-${var.account_id}"
    validation  = "rtdp-validation-${var.account_id}"
  }
}

resource "aws_s3_bucket" "b" {
  for_each      = local.buckets
  bucket        = each.value
  force_destroy = false
  tags          = merge(var.tags, { Name = each.value, data_class = each.key })
}

resource "aws_s3_bucket_versioning" "b" {
  for_each = aws_s3_bucket.b
  bucket   = each.value.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "b" {
  for_each = aws_s3_bucket.b
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "b" {
  for_each                = aws_s3_bucket.b
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
# Per-owner-scope prefix policy is enforced by IAM conditions on workload
# roles (iam-workloads module), not on the bucket.
output "bucket_names" { value = { for k, v in aws_s3_bucket.b : k => v.bucket } }
output "bucket_arns" { value = { for k, v in aws_s3_bucket.b : k => v.arn } }
