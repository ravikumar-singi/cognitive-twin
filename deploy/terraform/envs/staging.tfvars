env                   = "staging"
region                = "ap-south-1"
domain                = "learn-staging.example.edu" # replace
certificate_arn       = "arn:aws:acm:ap-south-1:000000000000:certificate/replace-me"
oidc_issuer           = "https://login.example.edu" # the partner institution's IdP
oidc_client_id        = "cognitive-twin-staging"
alarm_email           = "ct-oncall@example.edu"
nat_gateways          = 1
desired_count         = 2
max_count             = 4
db_instance_class     = "db.t4g.medium"
db_multi_az           = false
db_read_replica       = false
backup_retention_days = 14
log_retention_days    = 30
demo_enabled          = true
validators = {
  "lighthouse-university-a"     = { name = "Lighthouse University A", stake = 40, generations = [1] }
  "lighthouse-university-b"     = { name = "Lighthouse University B", stake = 35, generations = [1] }
  "professional-licensing-body" = { name = "Professional Licensing Body", stake = 30, generations = [1] }
  "employer-consortium"         = { name = "Employer Consortium", stake = 25, generations = [1] }
}
