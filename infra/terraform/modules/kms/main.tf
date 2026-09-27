# KMS customer-managed keys per data class (spec section 3).
variable "tags" {
  type = map(string)
}
resource "aws_kms_key" "data" {
  description             = "RTDP sandbox data-class key (stores, topics)"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = var.tags
}
resource "aws_kms_alias" "data" {
  name          = "alias/rtdp-sandbox-data"
  target_key_id = aws_kms_key.data.key_id
}
resource "aws_kms_key" "artifacts" {
  description             = "RTDP sandbox artifacts (models, bundles, checkpoints)"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = var.tags
}
resource "aws_kms_alias" "artifacts" {
  name          = "alias/rtdp-sandbox-artifacts"
  target_key_id = aws_kms_key.artifacts.key_id
}
output "data_key_arn" {
  value = aws_kms_key.data.arn
}
output "artifacts_key_arn" {
  value = aws_kms_key.artifacts.arn
}