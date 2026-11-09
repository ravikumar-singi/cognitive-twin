resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_parameter_group" "pg16" {
  name   = "${local.name}-pg16"
  family = "postgres16"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "500"
  }
  parameter {
    name  = "log_connections"
    value = "1"
  }
  # Safety net (also set per connection by the app): end transactions left idle,
  # so a leaked session can never pin a connection indefinitely.
  parameter {
    name  = "idle_in_transaction_session_timeout"
    value = "30000"
  }
}

# R1.1 managed PostgreSQL with point-in-time recovery; R1.5 encrypted daily backups.
resource "aws_db_instance" "main" {
  identifier                          = local.name
  engine                              = "postgres"
  engine_version                      = "16.4"
  instance_class                      = var.db_instance_class
  allocated_storage                   = 50
  max_allocated_storage               = 500
  storage_type                        = "gp3"
  storage_encrypted                   = true
  kms_key_id                          = aws_kms_key.data.arn
  db_name                             = "cognitive_twin"
  username                            = "ct_owner"
  manage_master_user_password         = true
  master_user_secret_kms_key_id       = aws_kms_key.data.arn
  db_subnet_group_name                = aws_db_subnet_group.main.name
  vpc_security_group_ids              = [aws_security_group.db.id]
  parameter_group_name                = aws_db_parameter_group.pg16.name
  multi_az                            = var.db_multi_az
  publicly_accessible                 = false
  backup_retention_period             = var.backup_retention_days
  backup_window                       = "19:00-20:00" # 00:30-01:30 IST
  maintenance_window                  = "sun:20:30-sun:21:30"
  copy_tags_to_snapshot               = true
  deletion_protection                 = true
  skip_final_snapshot                 = false
  final_snapshot_identifier           = "${local.name}-final"
  performance_insights_enabled        = true
  performance_insights_kms_key_id     = aws_kms_key.data.arn
  auto_minor_version_upgrade          = true
  ca_cert_identifier                  = "rds-ca-rsa2048-g1"
  enabled_cloudwatch_logs_exports     = ["postgresql"]
  iam_database_authentication_enabled = false
  apply_immediately                   = false
}

resource "aws_db_instance" "replica" {
  count                           = var.db_read_replica ? 1 : 0
  identifier                      = "${local.name}-replica"
  replicate_source_db             = aws_db_instance.main.identifier
  instance_class                  = var.db_instance_class
  storage_encrypted               = true
  kms_key_id                      = aws_kms_key.data.arn
  vpc_security_group_ids          = [aws_security_group.db.id]
  parameter_group_name            = aws_db_parameter_group.pg16.name
  publicly_accessible             = false
  performance_insights_enabled    = true
  performance_insights_kms_key_id = aws_kms_key.data.arn
  skip_final_snapshot             = true
  auto_minor_version_upgrade      = true
}

# Password for the least-privilege role the service connects as (R1.2).
resource "random_password" "app_db" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "app_db" {
  name       = "${local.name}/db-app-password"
  kms_key_id = aws_kms_key.data.arn
}

resource "aws_secretsmanager_secret_version" "app_db" {
  secret_id     = aws_secretsmanager_secret.app_db.id
  secret_string = random_password.app_db.result
}

# --- AWS Backup: daily, encrypted, 35-day retention (R1.5) ------------------------------

resource "aws_backup_vault" "main" {
  name        = local.name
  kms_key_arn = aws_kms_key.data.arn
}

resource "aws_backup_plan" "main" {
  name = local.name
  rule {
    rule_name         = "daily"
    target_vault_name = aws_backup_vault.main.name
    schedule          = "cron(0 21 * * ? *)" # 02:30 IST
    lifecycle {
      delete_after = 35
    }
  }
}

resource "aws_iam_role" "backup" {
  name = "${local.name}-backup"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "backup.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "backup" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}

resource "aws_iam_role_policy_attachment" "restore" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores"
}

resource "aws_backup_selection" "db" {
  name         = "${local.name}-db"
  plan_id      = aws_backup_plan.main.id
  iam_role_arn = aws_iam_role.backup.arn
  resources    = [aws_db_instance.main.arn]
}
