# EKS: control plane + private node groups, cluster autoscaler-ready,
# Pod Identity for workload IAM (no IRSA OIDC hand-wiring per service).
variable "tags" {
  type = map(string)
}
variable "permission_boundary_arn" {
  type = string
}
variable "system_nodes" {
  default = 1 # sandbox: 1; HA wants 2
}
variable "inference_nodes" {
  default = 1 # sandbox: 1; HA wants 2
}
variable "cluster_name" {
  default = "rtdp-sandbox"
}
variable "kubernetes_version" {
  default = "1.31"
}
variable "vpc_id" {
  type = string
}
variable "public_access_cidrs" {
  type    = list(string)
  default = [] # empty = private-only API; set to e.g. ["1.2.3.4/32"] for sandbox kubectl
}
variable "private_subnet_ids" {
  type = list(string)
}
resource "aws_eks_cluster" "this" {
  name     = var.cluster_name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn

  vpc_config {
    subnet_ids              = var.private_subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = length(var.public_access_cidrs) > 0
    public_access_cidrs     = var.public_access_cidrs
  }
  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]
  tags                      = var.tags
}
resource "aws_iam_role" "cluster" {
  name                 = "rtdp-eks-cluster"
  assume_role_policy   = data.aws_iam_policy_document.eks_assume.json
  permissions_boundary = var.permission_boundary_arn
  tags                 = var.tags
}
data "aws_iam_policy_document" "eks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}
resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}
resource "aws_iam_role" "nodes" {
  name                 = "rtdp-eks-nodes"
  assume_role_policy   = data.aws_iam_policy_document.ec2_assume.json
  permissions_boundary = var.permission_boundary_arn
  tags                 = var.tags
}
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}
resource "aws_iam_role_policy_attachment" "nodes" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
  ])
  role       = aws_iam_role.nodes.name
  policy_arn = each.value
}
# Two node groups: system (ArgoCD, observability) and cpu-inference (ONNX pods).
resource "aws_eks_node_group" "system" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "system"
  node_role_arn   = aws_iam_role.nodes.arn
  subnet_ids      = var.private_subnet_ids
  instance_types  = ["t3.large"]
  scaling_config {
    desired_size = var.system_nodes
    min_size     = 1
    max_size     = 4
  }
  labels = { "rtdp.io/pool" = "system"
  }
  tags = var.tags
}
resource "aws_eks_node_group" "inference" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "cpu-inference"
  node_role_arn   = aws_iam_role.nodes.arn
  subnet_ids      = var.private_subnet_ids
  instance_types  = ["c7i.xlarge"] # compute-optimized for ONNX per spec section 3
  scaling_config {
    desired_size = var.inference_nodes
    min_size     = 1
    max_size     = 6
  }
  labels = { "rtdp.io/pool" = "cpu-inference"
  }
  taint {
    key    = "rtdp.io/inference"
    value  = "cpu"
    effect = "NO_SCHEDULE"
  }
  tags = var.tags
}
# Pod Identity agent addon — services get IAM via EksPodIdentityAssociation
resource "aws_eks_addon" "pod_identity" {
  cluster_name = aws_eks_cluster.this.name
  addon_name   = "eks-pod-identity-agent"
}
resource "aws_eks_addon" "vpc_cni" {
  cluster_name = aws_eks_cluster.this.name
  addon_name   = "vpc-cni"
}
resource "aws_eks_addon" "coredns" {
  cluster_name = aws_eks_cluster.this.name
  addon_name   = "coredns"
}
resource "aws_eks_addon" "kube_proxy" {
  cluster_name = aws_eks_cluster.this.name
  addon_name   = "kube-proxy"
}
output "cluster_name" {
  value = aws_eks_cluster.this.name
}
output "cluster_endpoint" {
  value = aws_eks_cluster.this.endpoint
}
output "cluster_ca" {
  value = aws_eks_cluster.this.certificate_authority[0].data
}
output "node_security_group_id" {
  value = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
}