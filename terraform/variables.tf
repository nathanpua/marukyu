variable "region" {
  default = "ap-southeast-1"
}

variable "instance_type" {
  default = "t2.micro"
}

variable "poll_interval" {
  default     = 60
  description = "Base poll interval in seconds"
}

variable "telegram_bot_token" {
  type        = string
  sensitive   = true
  default     = ""
  description = "Telegram bot token. Must be set together with telegram_chat_ids."
}

variable "telegram_chat_ids" {
  type        = list(string)
  sensitive   = true
  default     = []
  description = "List of Telegram chat IDs. Must be set together with telegram_bot_token."
}

variable "telegram_alarm_email" {
  type        = string
  default     = ""
  description = "Email for scheduler error alarms. Empty disables notifications."
}

variable "monitor_urls" {
  type        = list(string)
  description = "Pages to monitor. Each entry is a URL."
  default = [
    "https://www.marukyu-koyamaen.co.jp/english/shop/products/catalog/matcha/principal",
  ]
}
