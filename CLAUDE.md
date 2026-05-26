# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Cloudflare-bypassing stock monitor for Marukyu Koyamaen matcha pages. It runs on a free-tier EC2 instance and sends Telegram alerts when stock status changes, including per-size variation breakdown on restock.

## Commands

### Run locally (test mode)
```bash
# Single fetch to verify scraping works (bypasses CF once, prints all products)
python monitor_light.py --once

# Continuous monitor with macOS notifications
python monitor_light.py --macos-notify

# With Telegram
python monitor_light.py --telegram-bot-token <TOKEN> --telegram-chat-id <CHAT_ID>

# Debug logging
python monitor_light.py --once --debug
```

### Terraform (run from `terraform/`)
```bash
terraform init
terraform apply -var="telegram_bot_token=<TOKEN>" -var="telegram_chat_id=<ID>"
terraform destroy

# Force reprovision after changing monitor_light.py.
# WARNING: this destroys /opt/marukyu-monitor/state.json on the EBS root volume.
# The first stock transition after redeploy is absorbed as "Initial state recorded"
# rather than firing an alert.
terraform taint aws_instance.monitor && terraform apply
```

### Operational commands
```bash
# Connect to instance (requires session-manager-plugin installed locally)
$(terraform output -raw ssm_session_command)

# Tail logs on instance
sudo tail -f /var/log/marukyu-monitor/monitor.log
sudo journalctl -u marukyu-monitor -f
```

### In-place script deployment (preferred — preserves state.json)
```bash
# Encode and upload monitor_light.py in 3000-char base64 chunks via SSM send-command,
# then: python3 -m py_compile to verify syntax, cp to /opt/marukyu-monitor/, systemctl restart.
# Use this instead of terraform taint whenever only monitor_light.py changed.
# terraform taint is only needed if user_data.sh.tftpl or Python deps change.
```

## Architecture

### Two-phase Cloudflare bypass

The core challenge is that Cloudflare's `cf_clearance` cookie expires every ~30 minutes. Running Chromium continuously costs ~690 MB RAM (too much for t2.micro). The solution:

1. **Solve phase** (every 25 min): `scrapling.StealthySession` launches headless Chromium via `patchright`, solves the Cloudflare Turnstile, extracts `cf_clearance` + `User-Agent`, then closes Chromium immediately. Peak RAM ~200 MB, duration ~13 s.
2. **Poll phase** (every 600 s ±60 s jitter): `curl_cffi` replays cached cookies with a Chrome TLS fingerprint. No browser needed. Steady-state RAM ~58 MB, duration ~0.2 s.

### Key constants (`monitor_light.py`)
| Constant | Value | Purpose |
|---|---|---|
| `DEFAULT_POLL_INTERVAL` | 600 s | Base poll interval |
| `POLL_JITTER` | 60 s | Random ±offset added each cycle to avoid mechanical fingerprinting |
| `COOKIE_REFRESH_INTERVAL` | 1500 s (25 min) | Max cookie age before re-solving CF |
| `CF_SOLVE_TIMEOUT` | 120 000 ms | Chromium hard timeout |
| `MAX_RETRIES` | 3 | Retries for both solve and fetch |
| `HEARTBEAT_INTERVAL` | 86 400 s (24 h) | Daily Telegram "monitor online" message |
| `BURST_RESTOCK_THRESHOLD` | 5 | Restocks in one cycle that activate slow-poll mode |
| `BURST_POLL_INTERVAL` | 3600 s (1 h) | Poll interval for the rest of the day after a burst restock |
| `SIZE_TERMS_TTL` | 3600 s | How long to cache the WooCommerce size→slug mapping |
| `SHOP_BASE_URL` | `…/english/shop` | Base URL for WC AJAX and Store API calls |
| `PRODUCT_KANJI` | dict | Japanese names appended to English names in all notifications |

### Data flow
```
LightweightStockMonitor.run()
  → _needs_cookie_refresh()  →  _refresh_cookies()  →  solve_cloudflare()
  → _fetch(url)              →  fetch_lightweight()          (per UrlConfig)
  → parse_products(html)     →  regex against WooCommerce HTML
  → filter by watch_names    →  UrlConfig.watch_names (None = all products)
  → detect_changes()         →  diff against self.previous_state (dict[name → bool])
  → _handle_changes()
      on restock only:
        _get_size_terms()    →  fetch_size_terms()  →  WC Store API /attributes/1/terms
        fetch_variation_stock(product_url, cookies, size_terms)
          → GET product page → extract product_id from id="product-NNN"
          → POST ?wc-ajax=get_variation for each size slug
          → returns List[VariationStock] with is_in_stock per size
      → notify_telegram(change, variations) / notify_macos()
      if restocks > BURST_RESTOCK_THRESHOLD: set _slow_poll_date = today
  → _maybe_send_heartbeat()  →  notify_telegram_heartbeat()  (once per 24 h, skipped if changes fired)
  → _effective_poll_interval() → BURST_POLL_INTERVAL if _slow_poll_date == today, else poll_interval
```

**Multi-URL monitoring**: `--url URL` can be repeated. Each entry is `URL` (watch all) or `URL|Name1,Name2` (filter to named products). Configured in `terraform/variables.tf` → `monitor_urls`.

**`parse_products`**: pure regex against WooCommerce `<li class="product-type-*">` markup. Stock status from `instock` / `outofstock` CSS classes.

**Variation stock**: uses the public WooCommerce AJAX endpoint (`wc-ajax=get_variation`) — no authentication required. Returns `is_in_stock`, `availability_html`, `variation_id`, `sku` per size. Size→slug mapping cached from `/wp-json/wc/store/v1/products/attributes/1/terms`.

### Infrastructure (Terraform)
- **EC2**: t2.micro Ubuntu 22.04, `ap-southeast-1`, 8 GB gp3 EBS. IAM role grants only `logs:PutLogEvents` to the CloudWatch log group.
- **Lambda scheduler** (`terraform/lambda/scheduler.py`): Single function that start/stops the EC2 instance. Triggered by two EventBridge cron rules: start at 9:30 AM JST (Mon–Fri), stop at 5:30 PM JST (Mon–Fri).
- **Self-bootstrapping**: `user_data.sh.tftpl` is gzip-base64-encoded into EC2 user data (fits the 16 KB limit). It embeds `monitor_light.py` verbatim at deploy time via `${monitor_script}` template variable. **Changing `monitor_light.py` requires `terraform taint aws_instance.monitor`** to reprovision.
- **State**: S3 backend (`marukyu-tfstate-<account>/marukyu/terraform.tfstate`) with DynamoDB locking (`marukyu-tfstate-lock`). Created once by `terraform/bootstrap/`. **The Telegram bot token and chat ID are stored in plaintext inside the state object** despite `sensitive = true` — keep the bucket private (the bootstrap blocks public access and enables AES256 SSE).
- **Telegram vs Discord**: Telegram is used because Discord blocks AWS IP ranges (HTTP 403).

### AWS credentials
The provider uses the standard AWS SDK credential chain — no hardcoded profile. Export `AWS_PROFILE=<your-profile>` (or set `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`) before running `terraform`. Region defaults to `ap-southeast-1` via the `region` variable.
