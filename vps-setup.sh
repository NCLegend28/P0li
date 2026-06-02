#!/bin/bash
# OVHcloud Switzerland VPS Setup Script for Polymarket Bot
# Run this on your new VPS after SSHing in

set -e

echo "=== Polymarket Bot VPS Setup ==="
echo "Server: Switzerland (Zurich/Geneva)"
echo ""

# Update system
echo "[1/8] Updating system packages..."
apt update && apt upgrade -y

# Install dependencies
echo "[2/8] Installing Python, Git, and dependencies..."
apt install -y python3.12 python3-pip git curl ufw fail2ban

# Create bot user
echo "[3/8] Creating bot user..."
adduser --disabled-password --gecos "" botuser
usermod -aG sudo botuser

# Configure SSH (key-only, disable root login)
echo "[4/8] Securing SSH..."
sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/PermitRootLogin yes/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart sshd

# Setup firewall (note: Exoscale uses Security Groups too)
echo "[5/8] Configuring UFW firewall (Exoscale Security Groups are also recommended)..."
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 8765/tcp  # Bot web dashboard
ufw allow 8766/tcp  # Dashboard service
ufw --force enable

echo ""
echo "NOTE: Exoscale Security Groups (cloud firewall) + UFW (host firewall) = double protection"
echo "In Exoscale portal: Compute -> Security Groups -> Add rules for ports 22, 8765, 8766"
echo ""

# Install Python packages for bot
echo "[6/8] Installing Python packages..."
pip3 install --break-system-packages \
    langgraph langchain-core httpx pydantic pydantic-settings \
    loguru rich python-dotenv python-telegram-bot \
    py-clob-client web3

# Create app directory
echo "[7/8] Creating app directory..."
mkdir -p /home/botuser/polymarket-bot
chown botuser:botuser /home/botuser/polymarket-bot

# Setup logrotate for bot logs
echo "[8/8] Configuring log rotation..."
cat > /etc/logrotate.d/polybot << 'EOF'
/home/botuser/polymarket-bot/data/trades/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 0644 botuser botuser
}
EOF

echo ""
echo "=== Base setup complete! ==="
echo "Next steps:"
echo "1. Copy your SSH key to botuser: sudo su - botuser && mkdir -p ~/.ssh"
echo "2. Clone your repo: git clone <your-repo> polymarket-bot"
echo "3. Set up your .env file with production secrets"
echo "4. Run: sudo /home/botuser/polymarket-bot/setup-systemd.sh"
