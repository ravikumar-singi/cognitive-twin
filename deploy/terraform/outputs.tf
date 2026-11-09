output "url" {
  value = "https://${var.domain}"
}

output "alb_dns_name" {
  description = "Point var.domain at this (CNAME or alias record)"
  value       = aws_lb.main.dns_name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "cluster" {
  value = aws_ecs_cluster.main.name
}

output "service" {
  value = aws_ecs_service.app.name
}

output "migrate_task_definition" {
  value = aws_ecs_task_definition.migrate.arn
}

output "private_subnets" {
  value = aws_subnet.private[*].id
}

output "app_security_group" {
  value = aws_security_group.app.id
}

output "validator_kms_keys" {
  description = "Register each with: cognitive-twin keys rotate <validator> --kms-key-arn <arn>"
  value       = { for k, v in aws_kms_key.validator : k => v.arn }
}

output "prometheus_workspace" {
  value = aws_prometheus_workspace.main.prometheus_endpoint
}

output "db_endpoint" {
  value = aws_db_instance.main.address
}
