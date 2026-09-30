# RTDP sandbox environment — the only environment.
# Region locked by SCP + provider config; every resource tagged per policy.
# NOTE: uncommented modules land in the plan only as their stage is approved.

terraform {
  required_version = ">= 1.9"
  backend "s3" {
    bucket       = "rtdp-tfstate-079457921611"
    key          = "sandbox/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
    encrypt      = true
  }
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
}

variable "region" {
  default = "us-east-1"
}
variable "account_id" {
  type = string
}
variable "owner" {
  type = string
}
variable "permission_boundary_arn" {
  type = string
}
variable "alarm_email" {
  type    = string
  default = ""
}
locals {
  tags = {
    project    = "rtdp"
    env        = "sandbox"
    owner      = var.owner
    managed_by = "terraform"
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = local.tags
  }
}

# Stage B: foundation
module "network" {
  source = "../../modules/network"
  tags   = local.tags
}
module "kms" {
  source = "../../modules/kms"
  tags   = local.tags
}
module "s3" {
  source     = "../../modules/s3"
  tags       = local.tags
  kms_key    = module.kms.data_key_arn
  account_id = var.account_id
}
module "ecr" {
  source      = "../../modules/ecr"
  tags        = local.tags
  kms_key_arn = module.kms.artifacts_key_arn
}
module "observability" {
  source      = "../../modules/observability"
  tags        = local.tags
  alarm_email = var.alarm_email
}
# Stage C: data services — commented until Stage C is approved
module "msk" {
  source          = "../../modules/msk"
  tags            = local.tags
  vpc_id          = module.network.vpc_id
  subnet_ids      = module.network.data_subnet_ids
  kms_key_arn     = module.kms.data_key_arn
  broker_instance = "kafka.t3.small" # sandbox sizing; prod: kafka.m7g.large
}
module "aurora" {
  source              = "../../modules/aurora"
  tags                = local.tags
  deletion_protection = false # sandbox: teardown must be able to destroy
  vpc_id              = module.network.vpc_id
  subnet_ids          = module.network.data_subnet_ids
  kms_key_arn         = module.kms.data_key_arn
}
module "elasticache" {
  source      = "../../modules/elasticache"
  tags        = local.tags
  vpc_id      = module.network.vpc_id
  subnet_ids  = module.network.data_subnet_ids
  kms_key_arn = module.kms.data_key_arn
  node_type   = "cache.t4g.micro" # sandbox sizing; prod: cache.t4g.medium+
}
# Stage D: compute — commented until Stage D is approved
module "eks" {
  source                  = "../../modules/eks"
  tags                    = local.tags
  vpc_id                  = module.network.vpc_id
  private_subnet_ids      = module.network.private_subnet_ids
  permission_boundary_arn = var.permission_boundary_arn
  public_access_cidrs     = ["70.18.235.134/32"] # operator IP — sandbox kubectl
}
module "iam_workloads" {
  source                  = "../../modules/iam-workloads"
  tags                    = local.tags
  cluster_name            = module.eks.cluster_name
  msk_cluster_arn         = module.msk.cluster_arn
  bucket_arns             = module.s3.bucket_arns
  kms_data_key_arn        = module.kms.data_key_arn
  kms_artifacts_key_arn   = module.kms.artifacts_key_arn
  permission_boundary_arn = var.permission_boundary_arn
  secret_arns             = [module.aurora.master_secret_arn]
}
module "flink" {
  source                  = "../../modules/flink"
  tags                    = local.tags
  permission_boundary_arn = var.permission_boundary_arn
  kms_key_arns            = [module.kms.data_key_arn, module.kms.artifacts_key_arn]
  vpc_id                  = module.network.vpc_id
  subnet_ids              = module.network.data_subnet_ids
  security_group_ids      = [module.msk.security_group_id]
  checkpoint_bucket       = module.s3.bucket_names.checkpoints
  jar_bucket              = module.s3.bucket_names.artifacts
  kafka_bootstrap         = module.msk.bootstrap_brokers_iam
}