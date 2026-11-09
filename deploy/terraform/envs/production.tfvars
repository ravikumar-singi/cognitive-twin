env                   = "production"
region                = "ap-south-1"
domain                = "learn.example.edu" # replace
certificate_arn       = "arn:aws:acm:ap-south-1:000000000000:certificate/replace-me"
oidc_issuer           = "https://login.example.edu"
oidc_client_id        = "cognitive-twin"
alarm_email           = "ct-oncall@example.edu"
vpc_cidr              = "10.50.0.0/16"
nat_gateways          = 2
task_cpu              = 1024
task_memory           = 2048
desired_count         = 3
max_count             = 10
db_instance_class     = "db.r7g.large"
db_multi_az           = true
db_read_replica       = true
backup_retention_days = 35
log_retention_days    = 365
demo_enabled          = false
validators = {
  "lighthouse-university-a"     = { name = "Lighthouse University A", stake = 40, generations = [1] }
  "lighthouse-university-b"     = { name = "Lighthouse University B", stake = 35, generations = [1] }
  "professional-licensing-body" = { name = "Professional Licensing Body", stake = 30, generations = [1] }
  "employer-consortium"         = { name = "Employer Consortium", stake = 25, generations = [1] }
}
