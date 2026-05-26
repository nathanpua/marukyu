# One-time bootstrap: creates the S3 bucket + DynamoDB table that the main
# stack uses as its remote backend. Run this once with the local backend,
# then point terraform/main.tf at the resulting bucket/table and run
# `terraform init -migrate-state` from terraform/.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  backend "local" {
    path = "terraform.tfstate"
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  default = "ap-southeast-1"
}

data "aws_caller_identity" "current" {}

locals {
  bucket_name = "marukyu-tfstate-${data.aws_caller_identity.current.account_id}"
  table_name  = "marukyu-tfstate-lock"
}

resource "aws_s3_bucket" "tfstate" {
  bucket = local.bucket_name

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "tfstate_lock" {
  name         = local.table_name
  hash_key     = "LockID"
  billing_mode = "PAY_PER_REQUEST"

  attribute {
    name = "LockID"
    type = "S"
  }

  lifecycle {
    prevent_destroy = true
  }
}

output "bucket" {
  value = aws_s3_bucket.tfstate.bucket
}

output "dynamodb_table" {
  value = aws_dynamodb_table.tfstate_lock.name
}

output "backend_config" {
  description = "Copy these values into terraform/main.tf backend \"s3\" block"
  value = {
    bucket         = aws_s3_bucket.tfstate.bucket
    key            = "marukyu/terraform.tfstate"
    region         = var.region
    dynamodb_table = aws_dynamodb_table.tfstate_lock.name
    encrypt        = true
  }
}
