# Stage F wiring outputs — consumed by tools/aws/render-env.sh to generate
# deploy/helm/rtdp-service/values-aws-sandbox.yaml and bootstrap manifests.
output "msk_bootstrap_iam" {
  value = module.msk.bootstrap_brokers_iam
}
output "aurora_writer_endpoint" {
  value = module.aurora.writer_endpoint
}
output "aurora_master_secret_arn" {
  value = module.aurora.master_secret_arn
}
output "aurora_database_name" {
  value = "rtdp"
}
output "valkey_endpoint" {
  value = module.elasticache.primary_endpoint
}
output "eks_cluster_name" {
  value = module.eks.cluster_name
}
output "flink_app_name" {
  value = "rtdp-features-sandbox"
}
output "bucket_names" {
  value = module.s3.bucket_names
}
output "ecr_registry" {
  value = "079457921611.dkr.ecr.us-east-1.amazonaws.com"
}
