# ECR repos with scan-on-push; images deploy by digest (spec section 3).
variable "tags" {
  type = map(string)
}
variable "repos" {
  type = list(string)
  default = [
    "ingress", "orchestrator", "feature-service", "signal-resolver",
    "rules-service", "inference-service", "feature-materializer",
    "action-dispatcher", "projector", "event-api",
  ]
}

resource "aws_ecr_repository" "svc" {
  for_each             = toset(var.repos)
  name                 = "rtdp/${each.value}"
  image_tag_mutability = "IMMUTABLE" # digests only; ArgoCD deploys by digest
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "KMS"
  }
  tags = var.tags
}
resource "aws_ecr_lifecycle_policy" "svc" {
  for_each   = aws_ecr_repository.svc
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep last 20 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = {
        type = "expire"
      }
    }]
  })
}

output "repository_urls" { value = { for k, v in aws_ecr_repository.svc : k => v.repository_url } }
