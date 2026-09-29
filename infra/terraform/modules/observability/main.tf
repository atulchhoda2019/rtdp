# Observability: CloudWatch log groups + dashboards, AMP (managed Prometheus)
# for service metrics, SNS topic for alarms.
variable "tags" {
  type = map(string)
}
variable "name" {
  default = "rtdp-sandbox"
}
variable "alarm_email" {
  type    = string
  default = ""
}
resource "aws_cloudwatch_log_group" "services" {
  name              = "/rtdp/${var.name}/services"
  retention_in_days = 30
  tags              = var.tags
}
resource "aws_prometheus_workspace" "this" {
  alias = "rtdp-${var.name}"
  tags  = var.tags
}
resource "aws_sns_topic" "alarms" {
  name = "rtdp-${var.name}-alarms"
  tags = var.tags
}
resource "aws_sns_topic_subscription" "email" {
  count     = var.alarm_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}
# Core SLO alarms — decision p95, Kafka consumer lag, Flink restarts.
resource "aws_cloudwatch_metric_alarm" "kafka_lag" {
  alarm_name          = "rtdp-kafka-consumer-lag"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "SumOffsetLag"
  namespace           = "AWS/Kafka"
  period              = 60
  statistic           = "Maximum"
  threshold           = 10000
  alarm_description   = "Consumer lag exceeds 10k for 3m"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  tags                = var.tags
}
resource "aws_cloudwatch_dashboard" "rtdp" {
  dashboard_name = "rtdp-${var.name}"
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title = "Decision latency (target p95 < 150ms)"
          metrics = [["rtdp", "decision_latency_ms", {
          stat = "p95" }]]
          region = data.aws_region.current.region
          period = 60
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title = "Throughput (target 500 TPS)"
          metrics = [["rtdp", "decisions_total", {
          stat = "Sum" }]]
          region = data.aws_region.current.region
          period = 60
        }
      },
    ]
  })
}

data "aws_region" "current" {}

output "amp_workspace_endpoint" {
  value = aws_prometheus_workspace.this.prometheus_endpoint
}
output "alarm_topic_arn" {
  value = aws_sns_topic.alarms.arn
}
output "log_group" {
  value = aws_cloudwatch_log_group.services.name
}