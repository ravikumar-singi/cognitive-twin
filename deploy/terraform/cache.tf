# Shared rate-limit counters, so limits hold across instances (R1.4, R3.6).
resource "aws_elasticache_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "random_password" "redis" {
  length  = 48
  special = false
}

resource "aws_elasticache_replication_group" "main" {
  replication_group_id       = local.name
  description                = "Cognitive Twin rate limiting"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.redis_node_type
  num_cache_clusters         = local.is_prd ? 2 : 1
  automatic_failover_enabled = local.is_prd
  port                       = 6379
  subnet_group_name          = aws_elasticache_subnet_group.main.name
  security_group_ids         = [aws_security_group.redis.id]
  at_rest_encryption_enabled = true
  kms_key_id                 = aws_kms_key.data.arn
  transit_encryption_enabled = true
  auth_token                 = random_password.redis.result
}

resource "aws_secretsmanager_secret" "redis_url" {
  name       = "${local.name}/redis-url"
  kms_key_id = aws_kms_key.data.arn
}

resource "aws_secretsmanager_secret_version" "redis_url" {
  secret_id     = aws_secretsmanager_secret.redis_url.id
  secret_string = "rediss://:${random_password.redis.result}@${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0"
}

resource "random_password" "session" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "session" {
  name       = "${local.name}/session-secret"
  kms_key_id = aws_kms_key.data.arn
}

resource "aws_secretsmanager_secret_version" "session" {
  secret_id     = aws_secretsmanager_secret.session.id
  secret_string = random_password.session.result
}

resource "random_password" "metrics" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "metrics" {
  name       = "${local.name}/metrics-token"
  kms_key_id = aws_kms_key.data.arn
}

resource "aws_secretsmanager_secret_version" "metrics" {
  secret_id     = aws_secretsmanager_secret.metrics.id
  secret_string = random_password.metrics.result
}

# The OIDC client secret is issued by the institution; an operator sets its value once:
#   aws secretsmanager put-secret-value --secret-id ct-<env>/oidc-client-secret --secret-string ...
resource "aws_secretsmanager_secret" "oidc" {
  name       = "${local.name}/oidc-client-secret"
  kms_key_id = aws_kms_key.data.arn
}
