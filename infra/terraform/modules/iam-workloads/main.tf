# Workload IAM roles bound to K8s service accounts via EKS Pod Identity.
# Per-service, least-privilege, boundary-attached. No IAM users anywhere.
#
# ADR-011: the inference/model identities get READ-ONLY artifact access and
# zero write/activation/dispatch permissions — enforced here, not by policy docs.
variable "tags" {
  type = map(string)
}
variable "cluster_name" {
  type = string
}
variable "msk_cluster_arn" {
  type = string
}
variable "bucket_arns" {
  type = map(string)
}
variable "kms_data_key_arn" {
  type = string
}
variable "kms_artifacts_key_arn" {
  type = string
}
variable "permission_boundary_arn" {
  type = string
}
variable "secret_arns" {
  type        = list(string)
  default     = []
  description = "Secrets Manager ARNs the external-secrets identity may read"
}
locals {
  # MSK IAM resource ARNs are type-prefixed off the cluster name/uuid —
  # :topic/<cluster>/<uuid>/<topic>, :group/..., :transactional-id/... —
  # NOT <cluster-arn>/*.
  msk_topic_arns = "${replace(var.msk_cluster_arn, ":cluster/", ":topic/")}/rtdp.*"
  msk_group_arns = "${replace(var.msk_cluster_arn, ":cluster/", ":group/")}/rtdp-*"
  msk_txn_arns   = "${replace(var.msk_cluster_arn, ":cluster/", ":transactional-id/")}/rtdp-*"
  msk_all_arns   = "${replace(var.msk_cluster_arn, ":cluster/", ":topic/")}/*"
  msk_all_groups = "${replace(var.msk_cluster_arn, ":cluster/", ":group/")}/*"
  msk_all_txn    = "${replace(var.msk_cluster_arn, ":cluster/", ":transactional-id/")}/*"
  # service_account -> which capabilities it needs
  services = {
    ingress              = ["kafka-write-events"]
    orchestrator         = ["kafka-txn", "s3-bundles-read", "pg-connect"]
    feature-service      = ["kafka-read-features", "valkey", "pg-connect"]
    signal-resolver      = ["kafka-none", "pg-connect"]
    rules-service        = ["s3-bundles-read", "pg-connect"]
    inference-service    = ["s3-artifacts-read"] # ADR-011: read-only, nothing else
    feature-materializer = ["kafka-read-features", "valkey"]
    action-dispatcher    = ["kafka-txn", "pg-connect"]
    projector            = ["kafka-read-decisions", "pg-connect"]
    slm-service          = ["s3-artifacts-read"] # ADR-011: read-only, nothing else
    # Bootstrap Job identity: topic admin + seed uploads (models, bundles).
    rtdp-bootstrap = ["kafka-admin", "s3-artifacts-write", "s3-bundles-write", "pg-connect"]
    # event-api: no implementation yet — add back when the service exists.
  }
}

data "aws_iam_policy_document" "pod_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "svc" {
  for_each             = local.services
  name                 = "rtdp-${each.key}"
  assume_role_policy   = data.aws_iam_policy_document.pod_assume.json
  permissions_boundary = var.permission_boundary_arn
  tags = merge(var.tags, {
  service = each.key })
}

resource "aws_eks_pod_identity_association" "svc" {
  for_each        = local.services
  cluster_name    = var.cluster_name
  namespace       = "rtdp"
  service_account = each.key
  role_arn        = aws_iam_role.svc[each.key].arn
}
# --- capability policies ---
resource "aws_iam_role_policy" "kafka" {
  for_each = { for s, caps in local.services : s => caps
    if length([for c in caps : c if can(regex("^kafka-", c)) && c != "kafka-none"]) > 0
  }
  name = "rtdp-kafka"
  role = aws_iam_role.svc[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{
        Effect   = "Allow"
        Action   = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster"]
        Resource = [var.msk_cluster_arn]
      }],
      # WriteDataIdempotently + WriteTxnMarkers are CLUSTER-level
      # permissions — ignored on topic/group/transactional-id ARNs.
      # Kafka >=3.8 ends transactions via WriteTxnMarkers (MSK IAM auth docs).
      length([for c in each.value : c
        if contains(["kafka-txn", "kafka-write-events", "kafka-admin"], c)]) > 0 ? [{
        Effect   = "Allow"
        Action   = ["kafka-cluster:WriteDataIdempotently"]
        Resource = [var.msk_cluster_arn]
      }] : [],
      length([for c in each.value : c
        if contains(["kafka-txn", "kafka-admin"], c)]) > 0 ? [{
        Effect   = "Allow"
        Action   = ["kafka-cluster:WriteTxnMarkers"]
        Resource = [var.msk_cluster_arn]
      }] : [],
      contains(each.value, "kafka-txn") ? [{
        Effect = "Allow"
        Action = ["kafka-cluster:ReadData", "kafka-cluster:WriteData", "kafka-cluster:DescribeTopic",
          "kafka-cluster:CreateTopic", "kafka-cluster:AlterGroup", "kafka-cluster:DescribeGroup",
        "kafka-cluster:DescribeTransactionalId", "kafka-cluster:AlterTransactionalId"]
        Resource = [local.msk_topic_arns, local.msk_group_arns, local.msk_txn_arns]
      }] : [],
      contains(each.value, "kafka-write-events") ? [{
        Effect   = "Allow"
        Action   = ["kafka-cluster:WriteData", "kafka-cluster:DescribeTopic"]
        Resource = [local.msk_topic_arns]
      }] : [],
      contains(each.value, "kafka-read-features") ? [{
        Effect = "Allow"
        Action = ["kafka-cluster:ReadData", "kafka-cluster:DescribeTopic",
        "kafka-cluster:AlterGroup", "kafka-cluster:DescribeGroup"]
        Resource = [local.msk_topic_arns, local.msk_group_arns]
      }] : [],
      contains(each.value, "kafka-read-decisions") ? [{
        Effect = "Allow"
        Action = ["kafka-cluster:ReadData", "kafka-cluster:DescribeTopic",
        "kafka-cluster:AlterGroup", "kafka-cluster:DescribeGroup"]
        Resource = [local.msk_topic_arns, local.msk_group_arns]
      }] : [],
      contains(each.value, "kafka-admin") ? [{
        Effect = "Allow"
        Action = ["kafka-cluster:CreateTopic", "kafka-cluster:DeleteTopic", "kafka-cluster:DescribeTopic", "kafka-cluster:AlterTopic",
          "kafka-cluster:ReadData", "kafka-cluster:WriteData", "kafka-cluster:AlterGroup",
          "kafka-cluster:DescribeGroup", "kafka-cluster:DescribeConfigs", "kafka-cluster:AlterConfigs",
        "kafka-cluster:DescribeTransactionalId", "kafka-cluster:AlterTransactionalId"]
        Resource = [local.msk_all_arns, local.msk_all_groups, local.msk_all_txn]
      }] : []
    )
  })
}

resource "aws_iam_role_policy" "s3" {
  for_each = { for s, caps in local.services : s => caps
  if length([for c in caps : c if can(regex("^s3-", c))]) > 0 }
  name = "rtdp-s3"
  role = aws_iam_role.svc[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      for cap in each.value : {
        Effect = "Allow"
        Action = (endswith(cap, "-write") ?
          ["s3:PutObject", "s3:GetObject", "s3:ListBucket", "s3:AbortMultipartUpload"] :
        ["s3:GetObject", "s3:GetObjectVersion", "s3:ListBucket"])
        Resource = (can(regex("bundles", cap)) ?
          [var.bucket_arns.bundles, "${var.bucket_arns.bundles}/*"] :
        [var.bucket_arns.artifacts, "${var.bucket_arns.artifacts}/*"])
      } if can(regex("^s3-", cap))
    ]
  })
}

resource "aws_iam_role_policy" "kms" {
  for_each = local.services
  name     = "rtdp-kms"
  role     = aws_iam_role.svc[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = (length([for c in each.value : c if can(regex("-write$", c))]) > 0 ?
        ["kms:Decrypt", "kms:DescribeKey", "kms:Encrypt", "kms:GenerateDataKey"] :
      ["kms:Decrypt", "kms:DescribeKey"])
      Resource = [var.kms_data_key_arn, var.kms_artifacts_key_arn]
    }]
  })
}

# External Secrets Operator: pod identity in its own namespace, reads only
# the declared secret ARNs (Aurora managed master secret + rtdp/* secrets).
resource "aws_iam_role" "external_secrets" {
  count                = length(var.secret_arns) > 0 ? 1 : 0
  name                 = "rtdp-external-secrets"
  assume_role_policy   = data.aws_iam_policy_document.pod_assume.json
  permissions_boundary = var.permission_boundary_arn
  tags                 = merge(var.tags, { service = "external-secrets" })
}

resource "aws_eks_pod_identity_association" "external_secrets" {
  count           = length(var.secret_arns) > 0 ? 1 : 0
  cluster_name    = var.cluster_name
  namespace       = "external-secrets"
  service_account = "external-secrets"
  role_arn        = aws_iam_role.external_secrets[0].arn
}

resource "aws_iam_role_policy" "external_secrets" {
  count = length(var.secret_arns) > 0 ? 1 : 0
  name  = "rtdp-secrets-read"
  role  = aws_iam_role.external_secrets[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
      Resource = var.secret_arns
    }]
  })
}

output "role_arns" { value = { for k, v in aws_iam_role.svc : k => v.arn } }
