variable "region" {
  default = "ap-southeast-1"
}

variable "instance_type" {
  default = "t2.micro"
}

variable "poll_interval" {
  default     = 600
  description = "Base poll interval in seconds (±60 s jitter is added automatically)"
}

variable "telegram_bot_token" {
  type        = string
  sensitive   = true
  default     = ""
  description = "Telegram bot token for stock change notifications. Must be set together with telegram_chat_id."
}

variable "telegram_chat_id" {
  type        = string
  sensitive   = true
  default     = ""
  description = "Telegram chat ID for stock change notifications. Must be set together with telegram_bot_token."
}

variable "telegram_alarm_email" {
  type        = string
  default     = ""
  description = "Email to subscribe to scheduler error alarms. Empty disables the SNS topic."
}

variable "monitor_urls" {
  type        = list(string)
  description = "Pages to monitor. Each entry is 'URL' (watch all) or 'URL|Name1,Name2' (watch specific products)."
  default = [
    "https://www.marukyu-koyamaen.co.jp/english/shop/products/catalog/matcha/principal",
    "https://www.marukyu-koyamaen.co.jp/english/shop/products/catalog/matcha/gentei|Shin Matcha Hatsu Enishi",
  ]
}
