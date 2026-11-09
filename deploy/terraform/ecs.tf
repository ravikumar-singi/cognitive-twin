resource "aws_ecr_repository" "app" {
  name                 = "cognitive-twin"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.data.arn
  }
}

resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/${local.name}/app"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.data.arn
}

# --- IAM ---------------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          aws_secretsmanager_secret.app_db.arn, aws_secretsmanager_secret.redis_url.arn,
          aws_secretsmanager_secret.session.arn, aws_secretsmanager_secret.oidc.arn,
          aws_secretsmanager_secret.metrics.arn, aws_db_instance.main.master_user_secret[0].secret_arn,
        ]
      },
      { Effect = "Allow", Action = ["kms:Decrypt"], Resource = [aws_kms_key.data.arn] }
    ]
  })
}

# The running service may only sign with and read validator keys: nothing else in KMS.
resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "task" {
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["kms:Sign", "kms:GetPublicKey"]
        Resource = [for k in aws_kms_key.validator : k.arn]
      },
      { Effect = "Allow", Action = ["aps:RemoteWrite"], Resource = [aws_prometheus_workspace.main.arn] },
      {
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
        Resource = "*"
      }
    ]
  })
}

# --- task definitions -----------------------------------------------------------------------

locals {
  common_env = [
    { name = "CT_ENV", value = var.env },
    { name = "CT_PUBLIC_BASE_URL", value = "https://${var.domain}" },
    { name = "CT_ALLOWED_HOSTS", value = var.domain },
    { name = "CT_TRUSTED_PROXY_HOPS", value = "1" },
    { name = "CT_DEV_LOGIN", value = "false" },
    { name = "CT_DEMO_ENABLED", value = tostring(var.demo_enabled) },
    { name = "CT_KEY_PROVIDER", value = "aws-kms" },
    { name = "CT_CONSENT_POLICY_VERSION", value = var.consent_policy_version },
    { name = "AWS_REGION", value = var.region },
    { name = "OIDC_ISSUER", value = var.oidc_issuer },
    { name = "OIDC_CLIENT_ID", value = var.oidc_client_id },
    { name = "DB_HOST", value = aws_db_instance.main.address },
    { name = "DB_PORT", value = tostring(aws_db_instance.main.port) },
    { name = "DB_NAME", value = aws_db_instance.main.db_name },
    { name = "DB_SSLMODE", value = "require" },
  ]
  common_secrets = [
    { name = "REDIS_URL", valueFrom = aws_secretsmanager_secret.redis_url.arn },
    { name = "CT_SESSION_SECRET", valueFrom = aws_secretsmanager_secret.session.arn },
    { name = "OIDC_CLIENT_SECRET", valueFrom = aws_secretsmanager_secret.oidc.arn },
  ]
  app_secrets   = concat(local.common_secrets, [{ name = "DB_PASSWORD", valueFrom = aws_secretsmanager_secret.app_db.arn }, { name = "CT_METRICS_TOKEN", valueFrom = aws_secretsmanager_secret.metrics.arn }])
  owner_secrets = concat(local.common_secrets, [{ name = "DB_PASSWORD", valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:password::" }, { name = "CT_APP_DB_PASSWORD", valueFrom = aws_secretsmanager_secret.app_db.arn }])
  log_config = {
    logDriver = "awslogs"
    options = {
      awslogs-group         = aws_cloudwatch_log_group.app.name
      awslogs-region        = var.region
      awslogs-stream-prefix = "app"
    }
  }
  adot_config = <<-EOT
    extensions:
      sigv4auth:
        region: ${var.region}
        service: aps
    receivers:
      prometheus:
        config:
          scrape_configs:
            - job_name: cognitive-twin
              scrape_interval: 15s
              metrics_path: /metrics
              authorization:
                credentials: $${env:CT_METRICS_TOKEN}
              static_configs:
                - targets: ["127.0.0.1:8000"]
      otlp:
        protocols:
          http:
            endpoint: 127.0.0.1:4318
    exporters:
      prometheusremotewrite:
        endpoint: ${aws_prometheus_workspace.main.prometheus_endpoint}api/v1/remote_write
        auth:
          authenticator: sigv4auth
        resource_to_telemetry_conversion:
          enabled: true
      awsxray:
        region: ${var.region}
    service:
      extensions: [sigv4auth]
      pipelines:
        metrics:
          receivers: [prometheus]
          exporters: [prometheusremotewrite]
        traces:
          receivers: [otlp]
          exporters: [awsxray]
  EOT
}

resource "aws_ecs_task_definition" "app" {
  family                   = "${local.name}-app"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([
    {
      name                   = "app"
      image                  = var.image
      essential              = true
      readonlyRootFilesystem = true
      user                   = "10001"
      command                = ["cognitive-twin", "serve", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--forwarded-allow-ips", "*"]
      portMappings           = [{ containerPort = 8000, protocol = "tcp" }]
      environment = concat(local.common_env, [
        { name = "DB_USER", value = "ct_app" },
        { name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = "http://127.0.0.1:4318" },
      ])
      secrets          = local.app_secrets
      logConfiguration = local.log_config
      healthCheck = {
        command     = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }
      mountPoints = [{ sourceVolume = "tmp", containerPath = "/tmp" }]
    },
    {
      name             = "adot"
      image            = "public.ecr.aws/aws-observability/aws-otel-collector:v0.40.0"
      essential        = false
      environment      = [{ name = "AOT_CONFIG_CONTENT", value = local.adot_config }]
      secrets          = [{ name = "CT_METRICS_TOKEN", valueFrom = aws_secretsmanager_secret.metrics.arn }]
      logConfiguration = merge(local.log_config, { options = merge(local.log_config.options, { awslogs-stream-prefix = "adot" }) })
    }
  ])
  volume {
    name = "tmp"
  }
}

# One-off task run by the deploy pipeline before each release: migrations as the
# owner, then (re)apply the least-privilege grants for ct_app.
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions = jsonencode([{
    name             = "migrate"
    image            = var.image
    essential        = true
    command          = ["sh", "-c", "cognitive-twin migrate && cognitive-twin db create-app-role"]
    environment      = concat(local.common_env, [{ name = "DB_USER", value = "ct_owner" }])
    secrets          = local.owner_secrets
    logConfiguration = merge(local.log_config, { options = merge(local.log_config.options, { awslogs-stream-prefix = "migrate" }) })
  }])
}

# Nightly full hash-chain verification (R1.2).
resource "aws_ecs_task_definition" "ledger_verify" {
  family                   = "${local.name}-ledger-verify"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions = jsonencode([{
    name             = "ledger-verify"
    image            = var.image
    essential        = true
    command          = ["cognitive-twin", "ledger", "verify"]
    environment      = concat(local.common_env, [{ name = "DB_USER", value = "ct_app" }])
    secrets          = local.app_secrets
    logConfiguration = merge(local.log_config, { options = merge(local.log_config.options, { awslogs-stream-prefix = "ledger" }) })
  }])
}

# --- service ------------------------------------------------------------------------------------

resource "aws_ecs_service" "app" {
  name                              = "app"
  cluster                           = aws_ecs_cluster.main.id
  task_definition                   = aws_ecs_task_definition.app.arn
  desired_count                     = var.desired_count
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 60
  enable_execute_command            = false
  propagate_tags                    = "SERVICE"
  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = 8000
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  lifecycle {
    ignore_changes = [desired_count]
  }
  depends_on = [aws_lb_listener.https]
}

resource "aws_appautoscaling_target" "app" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.app.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.desired_count
  max_capacity       = var.max_count
}

resource "aws_appautoscaling_policy" "cpu" {
  name               = "${local.name}-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.app.service_namespace
  resource_id        = aws_appautoscaling_target.app.resource_id
  scalable_dimension = aws_appautoscaling_target.app.scalable_dimension
  target_tracking_scaling_policy_configuration {
    target_value = 55
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

# --- nightly ledger check schedule --------------------------------------------------------------

resource "aws_iam_role" "scheduler" {
  name = "${local.name}-scheduler"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "scheduler.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecs:RunTask"], Resource = [aws_ecs_task_definition.ledger_verify.arn_without_revision, "${aws_ecs_task_definition.ledger_verify.arn_without_revision}:*"] },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = [aws_iam_role.execution.arn, aws_iam_role.task.arn] }
    ]
  })
}

resource "aws_scheduler_schedule" "ledger_verify" {
  name                         = "${local.name}-ledger-verify"
  schedule_expression          = "cron(30 20 * * ? *)" # 02:00 IST
  schedule_expression_timezone = "UTC"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.scheduler.arn
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.ledger_verify.arn_without_revision
      launch_type         = "FARGATE"
      network_configuration {
        subnets          = aws_subnet.private[*].id
        security_groups  = [aws_security_group.app.id]
        assign_public_ip = false
      }
    }
    retry_policy {
      maximum_retry_attempts = 2
    }
  }
}
