# Data encryption key: RDS, backups, secrets, logs.
resource "aws_kms_key" "data" {
  description             = "${local.name} data encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "Account"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "CloudWatchLogs"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.region}.amazonaws.com" }
        Action    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = "*"
      }
    ]
  })
}

resource "aws_kms_alias" "data" {
  name          = "alias/${local.name}-data"
  target_key_id = aws_kms_key.data.key_id
}

# Validator signing keys (R2.1). The private key never leaves KMS. One key per
# validator per generation; old generations are kept so retired keys remain
# available and credentials signed with them still verify (R2.3).
locals {
  validator_keys = merge([
    for vid, v in var.validators : {
      for g in v.generations : "${vid}-g${g}" => { validator = vid, generation = g }
    }
  ]...)
}

resource "aws_kms_key" "validator" {
  for_each                 = local.validator_keys
  description              = "${local.name} validator ${each.value.validator} generation ${each.value.generation}"
  customer_master_key_spec = "ECC_NIST_P256"
  key_usage                = "SIGN_VERIFY"
  deletion_window_in_days  = 30
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "validator" {
  for_each      = local.validator_keys
  name          = "alias/${local.name}-validator-${each.key}"
  target_key_id = aws_kms_key.validator[each.key].key_id
}
