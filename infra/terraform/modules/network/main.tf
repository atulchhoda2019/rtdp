# VPC: 3 AZs; private subnets for workloads, public only for LBs/NAT.
variable "tags" {
  type = map(string)
}
variable "cidr" {
  default = "10.40.0.0/16"
}
variable "azs" {
  type    = list(string)
  default = []
}
data "aws_availability_zones" "azs" {
  state = "available"
}
locals {
  azs  = length(var.azs) > 0 ? var.azs : slice(data.aws_availability_zones.azs.names, 0, 3)
  pub  = [for i in range(3) : cidrsubnet(var.cidr, 8, i)]
  priv = [for i in range(3) : cidrsubnet(var.cidr, 8, i + 10)]
  data = [for i in range(3) : cidrsubnet(var.cidr, 8, i + 20)]
}
resource "aws_vpc" "this" {
  cidr_block           = var.cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = merge(var.tags, { Name = "rtdp-sandbox" })
}

resource "aws_subnet" "public" {
  count                   = 3
  vpc_id                  = aws_vpc.this.id
  cidr_block              = local.pub[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false
  tags = merge(var.tags, { Name = "rtdp-public-${count.index}",
  "kubernetes.io/role/elb" = "1" })
}

resource "aws_subnet" "private" {
  count             = 3
  vpc_id            = aws_vpc.this.id
  cidr_block        = local.priv[count.index]
  availability_zone = local.azs[count.index]
  tags = merge(var.tags, { Name = "rtdp-private-${count.index}",
  "kubernetes.io/role/internal-elb" = "1" })
}

resource "aws_subnet" "data" {
  count             = 3
  vpc_id            = aws_vpc.this.id
  cidr_block        = local.data[count.index]
  availability_zone = local.azs[count.index]
  tags              = merge(var.tags, { Name = "rtdp-data-${count.index}" })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "rtdp-igw" })
}

resource "aws_eip" "nat" {
  count  = 3
  domain = "vpc"
  tags   = merge(var.tags, { Name = "rtdp-nat-${count.index}" })
}

resource "aws_nat_gateway" "this" {
  count         = 3
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  tags          = merge(var.tags, { Name = "rtdp-nat-${count.index}" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = merge(var.tags, { Name = "rtdp-public" })
}
resource "aws_route_table_association" "public" {
  count          = 3
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}
resource "aws_route_table" "private" {
  count  = 3
  vpc_id = aws_vpc.this.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this[count.index].id
  }
  tags = merge(var.tags, { Name = "rtdp-private-${count.index}" })
}
resource "aws_route_table_association" "private" {
  count          = 3
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}
resource "aws_route_table_association" "data" {
  count          = 3
  subnet_id      = aws_subnet.data[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}
# --- VPC endpoints: keep traffic off the internet (spec section 4) ---
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id
  tags              = var.tags
}
resource "aws_vpc_endpoint" "interface" {
  for_each = toset([
    "ecr.api", "ecr.dkr", "sts", "secretsmanager", "kms", "logs",
    "monitoring", "elasticache", "kafka", "rds", "eks",
  ])
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${data.aws_region.current.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
  tags                = var.tags
}
resource "aws_security_group" "endpoints" {
  name        = "rtdp-vpc-endpoints"
  vpc_id      = aws_vpc.this.id
  description = "VPC interface endpoints"
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.cidr]
  }
  tags = var.tags
}
data "aws_region" "current" {}

output "vpc_id" {
  value = aws_vpc.this.id
}
output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}
output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}
output "data_subnet_ids" {
  value = aws_subnet.data[*].id
}
output "azs" {
  value = local.azs
}