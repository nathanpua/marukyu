# Marukyu Koyamaen Matcha Stock Monitor

Polls multiple Marukyu Koyamaen matcha pages every 10 minutes, bypasses Cloudflare, and sends Telegram notifications when stock changes are detected. On restock, fetches per-size variation stock via the WooCommerce AJAX API. Runs on a free-tier AWS EC2 instance (Mon–Fri, 9:30 AM–5:30 PM JST).

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  AWS EC2 t2.micro (ap-southeast-1)                       │
│  Ubuntu 22.04 · 1 vCPU · 1 GB RAM · 8 GB EBS (gp3)      │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  systemd: marukyu-monitor.service                  │  │
│  │                                                    │  │
│  │  Every 25 min ──▶ Scrapling StealthyFetcher        │  │
│  │                   (Chromium headless, ~13s)         │  │
│  │                     │ extract cookies               │  │
│  │                     │ close Chromium                │  │
│  │                     ▼                               │  │
│  │  Every 10 min ──▶ curl_cffi (Chrome TLS, ~0.2s)    │  │
│  │  (1 hr if >5      │                                 │  │
│  │   items restock)  ▼                                 │  │
│  │            Parse HTML → Detect changes              │  │
│  │                     │                               │  │
│  │              ┌──────┴──────┐                        │  │
│  │              ▼             ▼                        │  │
│  │           Telegram     CloudWatch                   │  │
│  │          Bot API          Logs                      │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  1 GB swap (vm.swappiness=10)                            │
└──────────────────────────────────────────────────────────┘
```

### Two-Phase Fetch

Cloudflare's `cf_clearance` cookie expires in ~30 minutes. Rather than keeping Chromium running constantly (~690 MB RAM), the monitor uses a split strategy:

1. **Solve phase** (every 25 min): Launch headless Chromium, solve Cloudflare Turnstile, extract cookies + User-Agent, close Chromium. Peak RAM ~200 MB.
2. **Poll phase** (every 10 min ±60 s jitter): Use `curl_cffi` with Chrome TLS fingerprint impersonation + cached cookies. Steady-state RAM ~58 MB.

### Telegram Notifications

**Stock change alert** — sent whenever a product transitions between in-stock and out-of-stock:
- Product name (English + 日本語) with emoji (✅/❌)
- Price and status arrow (Out of Stock → In Stock)
- On restock: per-size package breakdown (e.g. ✅ 40g can, ❌ 20g can) fetched via WooCommerce AJAX
- Link to the product page

**Daily heartbeat** — sent once per day on startup, listing all currently in-stock products with Japanese names.

Telegram was chosen over Discord because Discord blocks requests from AWS IP addresses (HTTP 403).

### Burst-Restock Slow-Poll

If more than 5 products restock in a single poll cycle (indicating a large batch restock), the monitor automatically switches from 10-minute to 1-hour polling for the remainder of that day, then resets to normal the next day. This reduces unnecessary load on the site after a mass restock event.

### Why EBS

t2.micro has no instance store — EBS is required as the root volume. An 8 GB gp3 volume costs $0.72/month and persists across stop/start. The systemd service auto-resumes on boot via cloud-init provisioning.

### Self-Bootstrapping

The Terraform config embeds `monitor_light.py` and a full setup script into the instance's `user_data` (gzip-compressed to fit the 16 KB EC2 limit). On first boot, cloud-init runs all provisioning — no manual setup needed.

## Files

```
marukyu/
├── monitor_light.py          # Monitor application
└── terraform/
    ├── main.tf               # VPC, subnet, IGW, route table, SG, EC2, Lambda scheduler
    ├── variables.tf           # Region, instance type, poll interval, Telegram credentials
    ├── user_data.sh.tftpl     # Boot provisioning: swap, packages, venv, Chromium, systemd
    ├── lambda/
    │   └── scheduler.py       # Lambda for EC2 start/stop scheduling
    └── terraform.tfstate      # Local state
```

## Prerequisites

- AWS credentials available via the standard SDK chain (e.g. `export AWS_PROFILE=<your-profile>` or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env vars). Region defaults to `ap-southeast-1` via the `region` variable.
- Terraform >= 1.0

## Usage

### One-time backend bootstrap

The main stack uses an S3 backend with DynamoDB locking. Before the first `terraform init` on `terraform/`, create those resources:

```bash
cd marukyu/terraform/bootstrap
terraform init
terraform apply
# Note the bucket and dynamodb_table outputs; the values are hardcoded in terraform/main.tf
```

The bootstrap state is local (`terraform/bootstrap/terraform.tfstate`, gitignored). Both resources are marked `prevent_destroy = true`.

### Deploy

```bash
cd marukyu/terraform
terraform init
terraform apply \
  -var="telegram_bot_token=<YOUR_BOT_TOKEN>" \
  -var="telegram_chat_id=<YOUR_CHAT_ID>"
```

To deploy without Telegram notifications, omit the Telegram variables:

```bash
terraform apply -var="poll_interval=60"
```

`telegram_bot_token` and `telegram_chat_id` must be set together (precondition enforced by Terraform); setting only one fails the plan.

To receive an email when the scheduler Lambda errors or the DLQ fills, also pass:

```bash
terraform apply ... -var="telegram_alarm_email=you@example.com"
```

> **Secrets warning**: the local Terraform backend writes the Telegram bot token to `terraform/terraform.tfstate` in plaintext (`sensitive = true` only redacts plan output, not state files). The repo's `.gitignore` covers `*.tfstate*`, but treat the `terraform/` directory as a secrets-containing location — don't sync it to unencrypted cloud backups.

### Instance Access (SSM Session Manager)

```bash
# Requires session-manager-plugin: https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html
$(cd marukyu/terraform && terraform output -raw ssm_session_command)

# Or directly:
aws ssm start-session --target <instance-id> --region ap-southeast-1
```

### Check Logs

```bash
# On the instance:
sudo tail -f /var/log/marukyu-monitor/monitor.log
sudo journalctl -u marukyu-monitor -f
```

### Stop / Start

```bash
INSTANCE_ID=$(cd marukyu/terraform && terraform output -raw instance_id)

aws ec2 stop-instances  --instance-ids $INSTANCE_ID --region ap-southeast-1
aws ec2 start-instances --instance-ids $INSTANCE_ID --region ap-southeast-1
```

### Force Recreate

```bash
cd marukyu/terraform
terraform taint aws_instance.monitor
terraform apply
```

> **Caveat**: the EBS root volume is destroyed with the instance, including `/opt/marukyu-monitor/state.json`. The first stock transition after the new instance boots is silently absorbed as "Initial state recorded" rather than firing an alert.

### Teardown

```bash
cd marukyu/terraform
terraform destroy
```

## CI/CD

GitHub Actions workflows under `.github/workflows/`:

| Workflow | Trigger | What it does |
|---|---|---|
| `terraform-plan.yml` | PR + push to main (paths: `terraform/**`, `monitor_light.py`) | fmt-check, init, validate, plan; comments the plan on the PR |
| `terraform-apply.yml` | Manual `workflow_dispatch` only | init + plan + apply. Requires input `confirm = APPLY` to proceed. |

### Required GitHub repository secrets

| Secret | Required | Purpose |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | yes | IAM user with deploy perms |
| `AWS_SECRET_ACCESS_KEY` | yes | Matching secret |
| `TF_BACKEND_BUCKET` | yes | S3 bucket name for Terraform state (output from bootstrap) |
| `TELEGRAM_BOT_TOKEN` | no | Set together with `TELEGRAM_CHAT_ID` to enable Telegram alerts |
| `TELEGRAM_CHAT_ID` | no | See above |
| `TELEGRAM_ALARM_EMAIL` | no | Subscribes to scheduler error alarms |

Set them via `gh secret set <NAME>` or the GitHub UI (Settings → Secrets and variables → Actions).

## Contributing

1. Fork the repo and create a feature branch.
2. Run `terraform/bootstrap/` once in your own AWS account to get an S3 backend bucket, then copy `terraform/backend.hcl.example` → `terraform/backend.hcl` and fill in your values.
3. Test changes to `monitor_light.py` locally with `python monitor_light.py --once --debug`.
4. Open a PR — the `terraform plan` workflow will comment the diff automatically.

The monitor script (`monitor_light.py`) has no external dependencies beyond what is listed in `terraform/user_data.sh.tftpl`. Keep it that way: the goal is a single-file script that installs cleanly on a vanilla Ubuntu 22.04 box.

## Cost

| Resource | Monthly Cost |
|---|---|
| EC2 t2.micro (750 hr free tier) | $0 |
| EBS 8 GB gp3 | $0.72 |
| Data transfer (first 100 GB free) | $0 |
| Telegram Bot API | $0 |
| **Total** | **~$0.72/month** |
