#!/bin/bash
# Systemd service setup for Polymarket Bot
# Run this after cloning the repo and setting up .env

BOT_DIR="/home/botuser/polymarket-bot"
SERVICE_NAME="polybot"

echo "=== Setting up systemd service for Polymarket Bot ==="

# Create systemd service file
cat > /etc/systemd/system/${SERVICE_NAME}.service << EOF
[Unit]
Description=Polymarket Trading Bot
After=network.target

[Service]
Type=simple
User=botuser
Group=botuser
WorkingDirectory=${BOT_DIR}
Environment=PYTHONPATH=src
Environment=HEADLESS=true
ExecStart=/usr/bin/python3 -m polybot.cli
Restart=always
RestartSec=10
StandardOutput=append:${BOT_DIR}/data/trades/systemd.log
StandardError=append:${BOT_DIR}/data/trades/systemd.log

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd
systemctl daemon-reload

# Enable service to start on boot
systemctl enable ${SERVICE_NAME}.service

echo ""
echo "=== Systemd service created! ==="
echo ""
echo "Commands to manage the bot:"
echo "  sudo systemctl start ${SERVICE_NAME}     # Start bot"
echo "  sudo systemctl stop ${SERVICE_NAME}      # Stop bot"
echo "  sudo systemctl restart ${SERVICE_NAME}     # Restart bot"
echo "  sudo systemctl status ${SERVICE_NAME}      # Check status"
echo "  sudo journalctl -u ${SERVICE_NAME} -f      # Follow logs"
echo ""
echo "Bot logs: ${BOT_DIR}/data/trades/bot.log"
echo ""
