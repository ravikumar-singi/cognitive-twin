terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
  # Partial configuration: see envs/<env>.backend.hcl
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project     = "cognitive-twin"
      Environment = var.env
      ManagedBy   = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name   = "ct-${var.env}"
  azs    = slice(data.aws_availability_zones.available.names, 0, 2)
  is_prd = var.env == "production"
}
