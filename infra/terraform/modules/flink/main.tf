# Amazon Managed Service for Apache Flink — feature computation job.
# Checkpoints/savepoints land in the checkpoints bucket (s3 module).
variable "tags" {
  type = map(string)
}
variable "name" {
  default = "rtdp-features-sandbox"
}
variable "vpc_id" {
  type = string
}
variable "subnet_ids" {
  type = list(string)
}
variable "security_group_ids" {
  type = list(string)
}
variable "checkpoint_bucket" {
  type = string
}
variable "jar_bucket" {
  type = string
}
variable "jar_key" {
  default = "flink/rtdp-flink-features.jar"
}
variable "runtime" {
  default = "FLINK-1_20"
}
resource "aws_kinesisanalyticsv2_application" "features" {
  name                   = var.name
  runtime_environment    = var.runtime
  service_execution_role = aws_iam_role.flink.arn

  application_configuration {
    application_code_configuration {
      code_content {
        s3_content_location {
          bucket_arn = "arn:aws:s3:::${var.jar_bucket}"
          file_key   = var.jar_key
        }
      }
      code_content_type = "ZIPFILE"
    }
    environment_properties {
      property_group {
        property_group_id = "rtdp.features"
        property_map = {
          "kafka.bootstrap"     = "SET_BY_ARGO_OR_SSM"
          "kafka.topics.input"  = "rtdp.events.raw.v1"
          "kafka.topics.output" = "rtdp.features.contributions.v1"
          "checkpoint.dir"      = "s3://${var.checkpoint_bucket}/features/checkpoints"
          "savepoint.dir"       = "s3://${var.checkpoint_bucket}/features/savepoints"
        }
      }
    }

    vpc_configuration {
      subnet_ids         = var.subnet_ids
      security_group_ids = var.security_group_ids
    }
    flink_application_configuration {
      checkpoint_configuration {
        configuration_type            = "CUSTOM"
        checkpointing_enabled         = true
        checkpoint_interval           = 60000
        min_pause_between_checkpoints = 30000
      }
      monitoring_configuration {
        configuration_type = "CUSTOM"
        log_level          = "INFO"
        metrics_level      = "TASK"
      }
      parallelism_configuration {
        configuration_type   = "CUSTOM"
        parallelism          = 4
        parallelism_per_kpu  = 1
        auto_scaling_enabled = true
      }
    }

    application_snapshot_configuration {
      snapshots_enabled = true
    }
  }

  tags = var.tags
}
resource "aws_iam_role" "flink" {
  name               = "rtdp-flink-sandbox"
  assume_role_policy = data.aws_iam_policy_document.flink_assume.json
  tags               = var.tags
}
data "aws_iam_policy_document" "flink_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["kinesisanalytics.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "flink" {
  name = "rtdp-flink-runtime"
  role = aws_iam_role.flink.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { # checkpoints/savepoints + jar read
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket", "s3:GetBucketLocation"]
        Resource = ["arn:aws:s3:::${var.checkpoint_bucket}", "arn:aws:s3:::${var.checkpoint_bucket}/*",
        "arn:aws:s3:::${var.jar_bucket}", "arn:aws:s3:::${var.jar_bucket}/*"]
      },
      { # VPC ENI management for the app
        Effect = "Allow"
        Action = ["ec2:DescribeVpcs", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups",
          "ec2:DescribeDhcpOptions", "ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface",
        "ec2:DescribeNetworkInterfaces", "ec2:CreateNetworkInterfacePermission"]
        Resource = "*"
      },
      { # CloudWatch logs
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogGroups", "logs:DescribeLogStreams"]
        Resource = "*"
      },
      { # MSK IAM auth (cluster-level; topic ARNs scoped by iam-workloads)
        Effect   = "Allow"
        Action   = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["kafka-cluster:ReadData", "kafka-cluster:WriteData", "kafka-cluster:DescribeTopic",
          "kafka-cluster:DescribeGroup", "kafka-cluster:AlterGroup", "kafka-cluster:DescribeTransactionId",
        "kafka-cluster:WriteTransactionId", "kafka-cluster:WriteDataIdempotently"]
        Resource = "*"
      },
    ]
  })
}

output "application_name" {
  value = aws_kinesisanalyticsv2_application.features.name
}
output "application_arn" {
  value = aws_kinesisanalyticsv2_application.features.arn
}