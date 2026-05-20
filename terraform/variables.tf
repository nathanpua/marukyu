variable "aws_profile" {
  default = "marukyu"
}

variable "region" {
  default = "ap-southeast-1"
}

variable "instance_type" {
  default = "t2.micro"
}

variable "key_name" {
  default = "marukyu-monitor-key"
}

variable "poll_interval" {
  default     = 60
  description = "Poll interval in seconds"
}

variable "telegram_bot_token" {
  type        = string
  sensitive   = true
  default     = ""
  description = "Telegram bot token for stock change notifications"
}

variable "telegram_chat_id" {
  type        = string
  sensitive   = true
  default     = ""
  description = "Telegram chat ID for stock change notifications"
}
