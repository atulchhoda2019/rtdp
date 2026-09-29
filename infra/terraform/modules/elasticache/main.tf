# ElastiCache Valkey (sandbox Tier 1) — user-selected over MemoryDB.
# MemoryDB is the production discussion point per spec section 3.
variable "tags" {
  type = map(string)
}
variable "name" {
  default = "rtdp-sandbox"
}
variable "vpc_id" {
  type = string
}
variable "subnet_ids" {
  type = list(string)
}
variable "num_nodes" {
  type    = number
  default = 1 # sandbox: no replica
}
variable "node_type" {
  default = "cache.t4g.medium"
}
variable "engine_version" {
  # 8.x required: the Tier-1 Lua op uses HEXPIRE (hash-field expiry),
  # which landed in Valkey 8.0 / Redis 7.4 — 7.2 does not support it.
  default = "8.0"
}
variable "kms_key_arn" {
  type = string
}
resource "aws_security_group" "valkey" {
  name   = "rtdp-valkey"
  vpc_id = var.vpc_id
  ingress {
    from_port   = 6379
    to_port     = 6379
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/8"]
  }
  tags = var.tags
}
resource "aws_elasticache_subnet_group" "this" {
  name       = "rtdp-sandbox"
  subnet_ids = var.subnet_ids
  tags       = var.tags
}
resource "aws_elasticache_parameter_group" "this" {
  name   = "rtdp-valkey8"
  family = "valkey8"
  parameter {
    name  = "cluster-enabled"
    value = "no"
  }
  tags = var.tags
}
# Replication group: 1 primary + 1 replica across AZs, TLS in transit,
# KMS at rest. Lua atomicity verified in Stage D gate.
resource "aws_elasticache_replication_group" "this" {
  replication_group_id       = var.name
  description                = "RTDP sandbox Tier 1 (Valkey)"
  engine                     = "valkey"
  engine_version             = var.engine_version
  node_type                  = var.node_type
  num_cache_clusters         = var.num_nodes
  automatic_failover_enabled = var.num_nodes > 1
  multi_az_enabled           = var.num_nodes > 1
  parameter_group_name       = aws_elasticache_parameter_group.this.name
  subnet_group_name          = aws_elasticache_subnet_group.this.name
  security_group_ids         = [aws_security_group.valkey.id]
  port                       = 6379
  transit_encryption_enabled = true
  at_rest_encryption_enabled = true
  kms_key_id                 = var.kms_key_arn
  snapshot_retention_limit   = 3
  apply_immediately          = true
  tags                       = var.tags
}
output "primary_endpoint" {
  value = aws_elasticache_replication_group.this.primary_endpoint_address
}
output "reader_endpoint" {
  value = aws_elasticache_replication_group.this.reader_endpoint_address
}
output "security_group_id" {
  value = aws_security_group.valkey.id
}