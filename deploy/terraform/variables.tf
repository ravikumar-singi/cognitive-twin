variable "env" {
  description = "staging or production"
  type        = string
  validation {
    condition     = contains(["staging", "production"], var.env)
    error_message = "env must be staging or production."
  }
}

variable "region" {
  type = string
}

variable "domain" {
  description = "Public host name, e.g. learn.example.edu"
  type        = string
}

variable "certificate_arn" {
  description = "ACM certificate for var.domain"
  type        = string
}

variable "image" {
  description = "Container image URI with an immutable tag (set by the deploy pipeline)"
  type        = string
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "nat_gateways" {
  description = "1 for staging, 2 (one per AZ) for production"
  type        = number
  default     = 1
}

variable "oidc_issuer" {
  type = string
}

variable "oidc_client_id" {
  type = string
}

variable "consent_policy_version" {
  type    = string
  default = "2026-11-pilot-v1"
}

variable "task_cpu" {
  type    = number
  default = 1024
}

variable "task_memory" {
  type    = number
  default = 2048
}

variable "desired_count" {
  type    = number
  default = 2
}

variable "max_count" {
  type    = number
  default = 6
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "db_multi_az" {
  type    = bool
  default = false
}

variable "db_read_replica" {
  description = "Create a read replica (production). Its lag is alarmed on (R5.2)."
  type        = bool
  default     = false
}

variable "backup_retention_days" {
  type    = number
  default = 14
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.small"
}

variable "alarm_email" {
  description = "Where alerts go (on-call distribution list)"
  type        = string
}

variable "log_retention_days" {
  type    = number
  default = 90
}

variable "demo_enabled" {
  type    = bool
  default = false
}

variable "validators" {
  description = <<-EOT
    Validators and their signing-key generations. To rotate a key, append a new
    generation number, apply, then run `cognitive-twin keys rotate <id> --kms-key-arn <arn>`.
    Never remove an old generation: retired keys must stay available (R2.3).
  EOT
  type = map(object({
    name        = string
    stake       = number
    generations = list(number)
  }))
}
