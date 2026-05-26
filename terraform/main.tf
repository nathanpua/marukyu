terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }

  # Backend values are deployer-specific. Copy terraform/backend.hcl.example
  # to terraform/backend.hcl (gitignored), fill in your values, then run:
  #   terraform init -backend-config=backend.hcl
  backend "s3" {}
}

provider "aws" {
  region = var.region
  # Credentials resolved from the standard AWS SDK chain (env vars,
  # AWS_PROFILE, shared config). Set AWS_PROFILE=<your-profile>
  # before running terraform.
}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_ssm_parameter" "ubuntu_ami" {
  name = "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id"
}

data "aws_caller_identity" "current" {}

# ─── Network ────────────────────────────────────────────────────────

resource "aws_vpc" "monitor_vpc" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "marukyu-monitor-vpc"
  }
}

resource "aws_internet_gateway" "monitor_igw" {
  vpc_id = aws_vpc.monitor_vpc.id

  tags = {
    Name = "marukyu-monitor-igw"
  }
}

resource "aws_subnet" "monitor_subnet" {
  vpc_id                  = aws_vpc.monitor_vpc.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = {
    Name = "marukyu-monitor-subnet"
  }
}

resource "aws_route_table" "monitor_rt" {
  vpc_id = aws_vpc.monitor_vpc.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.monitor_igw.id
  }

  tags = {
    Name = "marukyu-monitor-rt"
  }
}

resource "aws_route_table_association" "monitor_rta" {
  subnet_id      = aws_subnet.monitor_subnet.id
  route_table_id = aws_route_table.monitor_rt.id
}

resource "aws_security_group" "monitor_sg" {
  name        = "marukyu-monitor-sg"
  description = "Security group for matcha stock monitor"
  vpc_id      = aws_vpc.monitor_vpc.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "marukyu-monitor-sg"
  }
}

# ─── CloudWatch Logs ────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "monitor" {
  name              = "/marukyu-monitor/logs"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "scheduler_lambda" {
  name              = "/aws/lambda/marukyu-monitor-scheduler"
  retention_in_days = 7
}

# ─── EC2 Instance IAM Role ──────────────────────────────────────────

resource "aws_iam_role" "ec2_instance" {
  name = "marukyu-monitor-ec2"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "ec2_cw_logs" {
  name = "cloudwatch-logs"
  role = aws_iam_role.ec2_instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams",
      ]
      Resource = "${aws_cloudwatch_log_group.monitor.arn}:*"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ec2_ssm_core" {
  role       = aws_iam_role.ec2_instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# ─── Telegram Secrets (SSM Parameter Store) ─────────────────────────

locals {
  telegram_enabled = var.telegram_bot_token != "" && var.telegram_chat_id != ""
}

resource "terraform_data" "telegram_pair_check" {
  lifecycle {
    precondition {
      condition     = (var.telegram_bot_token == "") == (var.telegram_chat_id == "")
      error_message = "telegram_bot_token and telegram_chat_id must both be set or both empty."
    }
  }
}

resource "aws_ssm_parameter" "telegram_bot_token" {
  count = local.telegram_enabled ? 1 : 0
  name  = "/marukyu-monitor/telegram/bot-token"
  type  = "SecureString"
  value = var.telegram_bot_token
}

resource "aws_ssm_parameter" "telegram_chat_id" {
  count = local.telegram_enabled ? 1 : 0
  name  = "/marukyu-monitor/telegram/chat-id"
  type  = "SecureString"
  value = var.telegram_chat_id
}

resource "aws_iam_role_policy" "ec2_ssm_secrets" {
  count = local.telegram_enabled ? 1 : 0
  name  = "ssm-telegram-secrets"
  role  = aws_iam_role.ec2_instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["ssm:GetParameter"]
        Resource = [
          aws_ssm_parameter.telegram_bot_token[0].arn,
          aws_ssm_parameter.telegram_chat_id[0].arn,
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ssm.${var.region}.amazonaws.com"
          }
        }
      },
    ]
  })
}

resource "aws_iam_instance_profile" "ec2" {
  name = "marukyu-monitor-ec2-profile"
  role = aws_iam_role.ec2_instance.name
}

# ─── EC2 Instance ───────────────────────────────────────────────────

resource "aws_instance" "monitor" {
  ami                    = data.aws_ssm_parameter.ubuntu_ami.value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.monitor_subnet.id
  vpc_security_group_ids = [aws_security_group.monitor_sg.id]

  iam_instance_profile = aws_iam_instance_profile.ec2.name

  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 1
  }

  user_data_base64 = base64gzip(templatefile("${path.module}/user_data.sh.tftpl", {
    monitor_script      = file("${path.module}/../monitor_light.py")
    monitor_urls        = var.monitor_urls
    poll_interval       = var.poll_interval
    log_group_name      = aws_cloudwatch_log_group.monitor.name
    region              = var.region
    telegram_enabled    = local.telegram_enabled
    ssm_bot_token_param = local.telegram_enabled ? aws_ssm_parameter.telegram_bot_token[0].name : ""
    ssm_chat_id_param   = local.telegram_enabled ? aws_ssm_parameter.telegram_chat_id[0].name : ""
  }))

  root_block_device {
    volume_size = 8
    volume_type = "gp3"
  }

  # user_data is only applied on first boot (cloud-init). Subsequent script
  # changes are deployed in-place via SSM; use `terraform taint aws_instance.monitor`
  # when a full reprovision is needed.
  lifecycle {
    ignore_changes = [user_data_base64]
  }

  tags = {
    Name = "marukyu-matcha-monitor"
  }
}

# ─── Lambda: Instance Scheduler ─────────────────────────────────────

data "archive_file" "scheduler_lambda" {
  type        = "zip"
  output_path = "${path.module}/lambda/scheduler.zip"
  source_file = "${path.module}/lambda/scheduler.py"
}

resource "aws_iam_role" "scheduler_lambda" {
  name = "marukyu-monitor-scheduler-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_ec2" {
  name = "ec2-start-stop"
  role = aws_iam_role.scheduler_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ec2:StartInstances",
          "ec2:StopInstances",
        ]
        Resource = aws_instance.monitor.arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.scheduler_dlq.arn
      },
    ]
  })
}

resource "aws_sqs_queue" "scheduler_dlq" {
  name                      = "marukyu-monitor-scheduler-dlq"
  message_retention_seconds = 1209600 # 14 days, the SQS max
}

resource "aws_lambda_function_event_invoke_config" "scheduler" {
  function_name                = aws_lambda_function.scheduler.function_name
  maximum_event_age_in_seconds = 3600
  maximum_retry_attempts       = 2

  destination_config {
    on_failure {
      destination = aws_sqs_queue.scheduler_dlq.arn
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "scheduler_dlq_depth" {
  alarm_name          = "marukyu-scheduler-dlq-depth"
  alarm_description   = "Scheduler Lambda DLQ has unprocessed failed invocations"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.scheduler_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.telegram_alarm_email != "" ? [aws_sns_topic.alarms[0].arn] : []
  ok_actions          = var.telegram_alarm_email != "" ? [aws_sns_topic.alarms[0].arn] : []
}

resource "aws_iam_role_policy_attachment" "scheduler_basic" {
  role       = aws_iam_role.scheduler_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_lambda_function" "scheduler" {
  filename         = data.archive_file.scheduler_lambda.output_path
  source_code_hash = data.archive_file.scheduler_lambda.output_base64sha256
  function_name    = "marukyu-monitor-scheduler"
  role             = aws_iam_role.scheduler_lambda.arn
  handler          = "scheduler.handler"
  runtime          = "python3.10"
  timeout          = 30

  environment {
    variables = {
      REGION = var.region
    }
  }
}

# ─── EventBridge: Start Schedule (9:30 AM JST = 00:30 UTC) ──────────

resource "aws_cloudwatch_event_rule" "start" {
  name                = "marukyu-monitor-start"
  schedule_expression = "cron(30 0 ? * MON-FRI *)"
  description         = "Start monitor instance at 9:30 AM JST (weekdays)"
}

resource "aws_cloudwatch_event_target" "start" {
  rule      = aws_cloudwatch_event_rule.start.name
  target_id = "StartInstance"
  arn       = aws_lambda_function.scheduler.arn

  input = jsonencode({
    action       = "start"
    instance_ids = [aws_instance.monitor.id]
  })
}

resource "aws_lambda_permission" "allow_start" {
  statement_id  = "AllowStartFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scheduler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.start.arn
}

# ─── EventBridge: Stop Schedule (5:30 PM JST = 08:30 UTC) ───────────

resource "aws_cloudwatch_event_rule" "stop" {
  name                = "marukyu-monitor-stop"
  schedule_expression = "cron(30 8 ? * MON-FRI *)"
  description         = "Stop monitor instance at 5:30 PM JST (weekdays)"
}

resource "aws_cloudwatch_event_target" "stop" {
  rule      = aws_cloudwatch_event_rule.stop.name
  target_id = "StopInstance"
  arn       = aws_lambda_function.scheduler.arn

  input = jsonencode({
    action       = "stop"
    instance_ids = [aws_instance.monitor.id]
  })
}

resource "aws_lambda_permission" "allow_stop" {
  statement_id  = "AllowStopFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scheduler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.stop.arn
}

# ─── SNS Alarm Topic (optional) ─────────────────────────────────────

resource "aws_sns_topic" "alarms" {
  count = var.telegram_alarm_email != "" ? 1 : 0
  name  = "marukyu-monitor-alarms"
}

resource "aws_sns_topic_subscription" "alarm_email" {
  count     = var.telegram_alarm_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alarms[0].arn
  protocol  = "email"
  endpoint  = var.telegram_alarm_email
}

# ─── CloudWatch Alarm: Lambda Scheduler Errors ──────────────────────

resource "aws_cloudwatch_metric_alarm" "scheduler_errors" {
  alarm_name          = "marukyu-scheduler-lambda-errors"
  alarm_description   = "Scheduler Lambda failed to start/stop the EC2 instance"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.scheduler.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.telegram_alarm_email != "" ? [aws_sns_topic.alarms[0].arn] : []
  ok_actions          = var.telegram_alarm_email != "" ? [aws_sns_topic.alarms[0].arn] : []
}

# ─── Outputs ────────────────────────────────────────────────────────

output "instance_public_ip" {
  value = aws_instance.monitor.public_ip
}

output "instance_id" {
  value = aws_instance.monitor.id
}

output "ssm_session_command" {
  value = "aws ssm start-session --target ${aws_instance.monitor.id} --region ${var.region}"
}

output "log_group" {
  value = aws_cloudwatch_log_group.monitor.name
}

output "lambda_function" {
  value = aws_lambda_function.scheduler.function_name
}
