#!/bin/bash
# Deploy script for Polymarket Bot with US Direct Trading
# Run this locally to push changes and deploy to VPS

set -e

VPS_USER="botuser"
VPS_HOST="${1:-YOUR_VPS_IP}"
VPS_KEY="${2:-~/.ssh/exoscale_polybot}"

if [ "$VPS_HOST" == "YOUR_VPS_IP" ]; then
    echo "Usage: ./deploy.sh <VPS_IP> [SSH_KEY_PATH]"
    echo "Example: ./deploy.sh 185.42.XXX.XXX ~/.ssh/exoscale_polybot"
    exit 1
fi

echo "=== Deploying Polymarket Bot to $VPS_HOST ==="

# 1. Commit and push changes
echo "[1/4] Pushing code to git..."
git add -A
git commit -m "Add US direct trading strategy + Exoscale VPS support" || true
git push

# 2. SSH to VPS and pull
echo "[2/4] Pulling on VPS..."
ssh -i "$VPS_KEY" "$VPS_USER@$VPS_HOST" "cd ~/polymarket-bot && git pull"

# 3. Install any new dependencies
echo "[3/4] Installing dependencies..."
ssh -i "$VPS_KEY" "$VPS_USER@$VPS_HOST" "
    cd ~/polymarket-bot
    source .venv/bin/activate
    pip install -e '.[dev]'
"

# 4. Restart the bot
echo "[4/4] Restarting bot..."
ssh -i "$VPS_KEY" "$VPS_USER@$VPS_HOST" "sudo systemctl restart polybot"

echo ""
echo "=== Deploy complete! ==="
echo "Check logs: ssh -i $VPS_KEY $VPS_USER@$VPS_HOST 'sudo journalctl -u polybot -f'"
