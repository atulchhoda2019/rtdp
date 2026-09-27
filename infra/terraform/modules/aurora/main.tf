# Aurora PostgreSQL: control metadata, versioned assets, activation,
# decision facts, action ledger, idempotency ledger.
variable "tags" {
  type = map(string)
}
variable "cluster_name" {
  default = "rtdp-sandbox"
}
variable "vpc_id" {
  type = string
}
variable "subnet_ids" {
  type = list(string)
}
variable "kms_key_arn" {
  type = string
}
variable "instance_class" {
  default = "db.t4g.medium"
}
variable "database_name" {
  default = "rtdp"
}
resource "aws_security_group" "aurora" {
  name   = "rtdp-aurora"
  vpc_id = var.vpc_id
  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/8"]
  }
  tags = var.tags
}
resource "aws_db_subnet_group" "this" {
  name       = "rtdp-sandbox"
  subnet_ids = var.subnet_ids
  tags       = var.tags
}
resource "aws_rds_cluster" "this" {
  cluster_identifier                  = var.cluster_name
  engine                              = "aurora-postgresql"
  engine_version                      = "16.4"
  database_name                       = var.database_name
  master_username                     = "rtdp_admin"
  manage_master_user_password         = true # secret lands in Secrets Manager
  db_subnet_group_name                = aws_db_subnet_group.this.name
  vpc_security_group_ids              = [aws_security_group.aurora.id]
  storage_encrypted                   = true
  kms_key_id                          = var.kms_key_arn
  backup_retention_period             = 7
  preferred_backup_window             = "03:00-04:00"
  deletion_protection                 = true
  skip_final_snapshot                 = false
  final_snapshot_identifier           = "${var.cluster_name}-final"
  enabled_cloudwatch_logs_exports     = ["postgresql"]
  iam_database_authentication_enabled = true
  tags                                = var.tags
}
resource "aws_rds_cluster_instance" "writer" {
  identifier          = "${var.cluster_name}-writer"
  cluster_identifier  = aws_rds_cluster.this.id
  instance_class      = var.instance_class
  engine              = aws_rds_cluster.this.engine
  engine_version      = aws_rds_cluster.this.engine_version
  publicly_accessible = false
  tags                = var.tags
}
resource "aws_rds_cluster_instance" "reader" {
  identifier          = "${var.cluster_name}-reader"
  cluster_identifier  = aws_rds_cluster.this.id
  instance_class      = var.instance_class
  engine              = aws_rds_cluster.this.engine
  engine_version      = aws_rds_cluster.this.engine_version
  publicly_accessible = false
  tags                = var.tags
}
output "writer_endpoint" {
  value = aws_rds_cluster.this.endpoint
}
output "reader_endpoint" {
  value = aws_rds_cluster.this.reader_endpoint
}
output "master_secret_arn" {
  value = aws_rds_cluster.this.master_user_secret[0].secret_arn
}
output "security_group_id" {
  value = aws_security_group.aurora.id
}