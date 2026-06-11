# Marukyu Koyamaen Matcha Stock Monitor

Polls the [Principal Matcha page](https://www.marukyu-koyamaen.co.jp/english/shop/products/catalog/matcha/principal) every 60 seconds, bypasses Cloudflare, and sends Telegram notifications to multiple chats when stock changes are detected for 11 products. Runs on an AWS EC2 Spot instance for cost optimization.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  AWS EC2 t2.micro Spot (ap-southeast-1)                   │
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
│  │  Every 60s ────▶ curl_cffi (Chrome TLS, ~0.2s)     │  │
│  │                     │                               │  │
│  │                     ▼                               │  │
│  │            Parse HTML → Detect changes              │  │
│  │                     │                               │  │
│  │              ┌──────┴──────┐                        │  │
│  │              ▼             ▼                        │  │
│  │           Telegram     CloudWatch                   │  │
│  │          Bot API          Logs                      │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  1 GB swap (vm.swappiness=10)                            │
│  Spot: stop on interruption · scheduler: weekdays only   │
└──────────────────────────────────────────────────────────┘
```

### Two-Phase Fetch

Cloudflare's `cf_clearance` cookie expires in ~30 minutes. Rather than keeping Chromium running constantly (~690 MB RAM), the monitor uses a split strategy:

1. **Solve phase** (every 25 min): Launch headless Chromium, solve Cloudflare Turnstile, extract cookies + User-Agent, close Chromium. Peak RAM ~200 MB.
2. **Poll phase** (every 60s): Use `curl_cffi` with Chrome TLS fingerprint impersonation + cached cookies. Steady-state RAM ~58 MB.

### Telegram Notifications

When a product changes stock status (in stock or out of stock), the monitor sends a Telegram message to all configured chat IDs via the Bot API with:

- Product name with emoji (✅ in stock, ❌ out of stock)
- Price
- Status change (e.g., Out of Stock → In Stock)
- Link to the product page

Telegram was chosen over Discord because Discord blocks requests from AWS IP addresses (HTTP 403). The Telegram Bot API has no such restriction and offers ~50-100ms latency from the `ap-southeast-1` region.

### Why EBS

t2.micro has no instance store — EBS is required as the root volume. An 8 GB gp3 volume costs $0.72/month and persists across stop/start. The systemd service auto-resumes on boot via cloud-init provisioning.

### Spot Instance

The instance runs as an EC2 Spot instance, which offers up to 70% discount compared to On-Demand pricing. For `t2.micro`, Spot interruptions are extremely rare. The instance is configured with `instance_interruption_behavior = "stop"` so that if interrupted, it stops (preserving EBS and the instance ID) rather than terminating. The Lambda scheduler can restart it on the next scheduled window.

### Self-Bootstrapping

The Terraform config embeds `monitor_light.py` and a full setup script into the instance's `user_data` (gzip-compressed to fit the 16 KB EC2 limit). On first boot, cloud-init runs all provisioning — no SSH needed.

### Dynamic Secret Fetching

The systemd unit uses a wrapper script (`/usr/local/bin/run-monitor.sh`) that fetches Telegram credentials from SSM Parameter Store on every service start. This means adding or removing chat IDs requires no instance replacement — just update the SSM parameter and restart the service. CI/CD handles this automatically (see [CI/CD](#cicd)).

## Security

- **No SSH**: Access is via AWS SSM Session Manager only (`aws ssm start-session`). The security group has no inbound SSH (22) or any other ingress rule.
- **Secrets in Parameter Store**: Telegram bot token and chat IDs are stored in SSM Parameter Store (SecureString), not in `user_data` or environment variables.
- **IMDSv2**: Instance metadata service enforces token-based access (`http_tokens = required`) to prevent SSRF-style credential theft.
- **No hardcoded AWS profile**: The instance uses the default SDK credential chain (EC2 instance role), not a hardcoded profile name.
- **Lambda DLQ**: The scheduler Lambda has a dead-letter queue and optional email alarms for error handling.
- **GitHub Actions CI/CD**: Terraform plan and apply run via GitHub Actions with manual confirmation gate. No secrets are stored in Terraform state.

## Files

```
marukyu/
├── monitor_light.py              # Monitor application
├── .github/
│   └── workflows/
│       ├── terraform-plan.yml    # PR-triggered plan + Infracost estimate
│       └── terraform-apply.yml   # Manual-dispatch apply with confirmation gate
└── terraform/
    ├── main.tf                   # VPC, subnet, IGW, route table, SG, EC2, IAM, SSM params, Lambda, backend config
    ├── variables.tf              # Region, instance type, poll interval, Telegram credentials, monitor URLs
    ├── user_data.sh.tftpl        # Boot provisioning: swap, packages, venv, Chromium, systemd, secret wrapper
    ├── backend.hcl               # S3 + DynamoDB remote backend config (gitignored)
    ├── backend.hcl.example       # Template for backend.hcl
    ├── lambda/
    │   ├── scheduler.py          # Lambda for EC2 start/stop scheduling
    │   └── scheduler.zip         # Deployment package
    └── bootstrap/
        ├── main.tf               # Bootstraps S3 backend + DynamoDB lock table
        └── terraform.tfstate     # Bootstrap state (local-only, gitignored)
```

## Prerequisites

- AWS CLI configured with a default profile or `AWS_PROFILE` in `ap-southeast-1`
- Terraform >= 1.0
- GitHub repo with secrets configured (for CI/CD): `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS`

## Usage

### Bootstrap S3 Backend (one-time)

Before deploying the monitor, provision the remote state backend:

```bash
cd marukyu/terraform/bootstrap
terraform init
terraform apply -var="aws_account_id=<YOUR_AWS_ACCOUNT_ID>"

# Note the outputs:
terraform output -raw s3_bucket       # → marukyu-tfstate-<ACCOUNT_ID>
terraform output -raw dynamodb_table  # → marukyu-tfstate-lock
```

Then create `marukyu/terraform/backend.hcl` from the template:

```bash
cp marukyu/terraform/backend.hcl.example marukyu/terraform/backend.hcl
# Edit backend.hcl with the bucket and table from above
```

### Deploy

```bash
cd marukyu/terraform
terraform init -backend-config=backend.hcl
terraform apply \
  -var="telegram_bot_token=<YOUR_BOT_TOKEN>" \
  -var='telegram_chat_ids=["<CHAT_ID_1>","<CHAT_ID_2>"]'
```

To deploy without Telegram notifications, omit both Telegram variables:

```bash
terraform apply -var="poll_interval=60"
```

### Access via SSM

```bash
aws ssm start-session --target $(cd marukyu/terraform && terraform output -raw instance_id) --region ap-southeast-1
```

### Check Logs

```bash
# On the instance (via SSM):
sudo tail -f /var/log/marukyu-monitor/monitor.log
sudo journalctl -u marukyu-monitor -f

# Or via CloudWatch:
aws logs tail /var/log/marukyu-monitor --region ap-southeast-1
```

### Adding/Removing Chat IDs (No Instance Replacement)

The monitor fetches secrets from SSM on every start. To change chat IDs:

1. Update the `TELEGRAM_CHAT_IDS` GitHub secret
2. Trigger the `terraform apply` workflow (manual dispatch, type `APPLY`)
3. CI updates the SSM parameter and restarts the service automatically

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

### Teardown

```bash
cd marukyu/terraform
terraform destroy
```

## CI/CD

GitHub Actions handles all infrastructure changes:

| Workflow | Trigger | What it does |
|---|---|---|
| `terraform-plan.yml` | PR to `main` | Runs `terraform plan` + Infracost estimate |
| `terraform-apply.yml` | Manual dispatch (`workflow_dispatch`) | Runs `terraform apply` with confirmation gate (`APPLY`), waits for SSM agent (~120s), then restarts the service |

Apply workflow flow:
1. User types `APPLY` in the confirmation input
2. CI runs `terraform apply` (updates SSM params if chat IDs changed)
3. CI waits up to 120s for SSM agent registration on the instance
4. CI sends `systemctl restart marukyu-monitor` via SSM Run Command — the wrapper script re-fetches secrets from SSM on start

## Cost

| Resource | Monthly Cost |
|---|---|
| EC2 t2.micro Spot (~40 hr/week) | ~$0.80 |
| EBS 8 GB gp3 | $0.72 |
| Data transfer (first 100 GB free) | $0 |
| Telegram Bot API | $0 |
| S3 (state bucket, <1 MB) | ~$0.01 |
| DynamoDB (lock table, <1 WCU) | ~$0.01 |
| Lambda (scheduler, free tier) | $0 |
| CloudWatch Logs (free tier) | $0 |
| **Total** | **~$1.54/month** |
