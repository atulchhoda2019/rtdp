# MSK provisioned: 3 brokers across 3 AZs, IAM auth + TLS, KMS at rest.
# Kafka transactions + read_committed verified in Stage C gate.

variable "tags" { type = map(string) }
variable "cluster_name" {
  type    = string
  default = "rtdp-sandbox"
}
variable "kafka_version" {
  type    = string
  default = "3.9.x"
}
variable "vpc_id" { type = string }
variable "subnet_ids" { type = list(string) } # data subnets
variable "kms_key_arn" { type = string }
variable "broker_instance" {
  type    = string
  default = "kafka.m7g.large"
}
variable "broker_volume_gb" {
  type    = number
  default = 100
}

resource "aws_security_group" "msk" {
  name   = "rtdp-msk"
  vpc_id = var.vpc_id
  ingress {
    from_port   = 9098 # IAM-authenticated TLS
    to_port     = 9098
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/8"]
    description = "MSK IAM TLS"
  }
  tags = var.tags
}

resource "aws_msk_cluster" "this" {
  cluster_name           = var.cluster_name
  kafka_version          = var.kafka_version
  number_of_broker_nodes = 3

  broker_node_group_info {
    instance_type   = var.broker_instance
    client_subnets  = var.subnet_ids
    security_groups = [aws_security_group.msk.id]
    storage_info {
      ebs_storage_info {
        volume_size = var.broker_volume_gb
      }
    }
  }

  encryption_info {
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
    encryption_at_rest_kms_key_arn = var.kms_key_arn
  }

  client_authentication {
    sasl {
      iam = true # IAM auth only — no scram users, no plaintext
    }
  }

  configuration_info {
    arn      = aws_msk_configuration.this.arn
    revision = aws_msk_configuration.this.latest_revision
  }

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.msk.name
      }
    }
  }

  tags = var.tags
}

resource "aws_msk_configuration" "this" {
  # Version in the name + create_before_destroy: MSK refuses to delete a
  # configuration that a cluster still references, so the new revision-set
  # must exist before the old one is detached and destroyed.
  name              = "rtdp-sandbox-cfg-${replace(var.kafka_version, ".", "-")}"
  kafka_versions    = [var.kafka_version]
  server_properties = <<-EOF
    auto.create.topics.enable=false
    default.replication.factor=3
    min.insync.replicas=2
    transactional.id.expiration.ms=604800000
    log.retention.hours=168
  EOF

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_cloudwatch_log_group" "msk" {
  name              = "/rtdp/msk/${var.cluster_name}"
  retention_in_days = 30
  tags              = var.tags
}

output "bootstrap_brokers_iam" {
  value = aws_msk_cluster.this.bootstrap_brokers_sasl_iam
}
output "cluster_arn" {
  value = aws_msk_cluster.this.arn
}
output "security_group_id" {
  value = aws_security_group.msk.id
}
