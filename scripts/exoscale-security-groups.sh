#!/bin/bash
# Exoscale Security Groups Setup for Polymarket Bot VPS
# Creates security group allowing SSH + bot dashboard access

set -e

SG_NAME="polymarket-bot-sg"
ZONE="ch-dk-2"  # Zurich (can change to ch-gva-2 for Geneva)

echo "=== Exoscale Security Group Setup ==="
echo ""

# Check exo CLI
if ! command -v exo &> /dev/null; then
    echo "Error: exo CLI not found in PATH"
    echo "Install: https://community.exoscale.com/tools/command-line-interface/"
    exit 1
fi

# Check auth
if ! exo config list &> /dev/null; then
    echo "Error: Not authenticated with Exoscale"
    echo "Run: exo config add"
    exit 1
fi

# Create security group if doesn't exist
echo "[1/2] Creating security group: $SG_NAME"
if ! exo compute security-group show "$SG_NAME" &> /dev/null; then
    exo compute security-group create "$SG_NAME" --zone "$ZONE"
    echo "Security group created"
else
    echo "Security group already exists"
fi

# Add SSH rule (port 22)
echo "[2/2] Adding SSH rule (port 22)..."
exo compute security-group rule add "$SG_NAME" \
    --protocol tcp \
    --network "0.0.0.0/0" \
    --port 22 \
    --description "SSH access" \
    --zone "$ZONE" || echo "SSH rule may already exist"

# Add bot dashboard rules (ports 8765, 8766) - adjust source network as needed
echo "Adding bot dashboard ports (8765, 8766)..."
exo compute security-group rule add "$SG_NAME" \
    --protocol tcp \
    --network "0.0.0.0/0" \
    --port 8765 \
    --description "Bot web dashboard" \
    --zone "$ZONE" || echo "Port 8765 rule may already exist"

exo compute security-group rule add "$SG_NAME" \
    --protocol tcp \
    --network "0.0.0.0/0" \
    --port 8766 \
    --description "Dashboard service" \
    --zone "$ZONE" || echo "Port 8766 rule may already exist"

# Add ICMP for ping (optional but useful)
exo compute security-group rule add "$SG_NAME" \
    --protocol icmp \
    --network "0.0.0.0/0" \
    --description "ICMP ping" \
    --zone "$ZONE" || echo "ICMP rule may already exist"

echo ""
echo "=== Security group configured ==="
echo "Name: $SG_NAME"
echo "Zone: $ZONE"
echo ""
echo "Rules:"
echo "  - SSH (22) from anywhere"
echo "  - Bot dashboard (8765) from anywhere"
echo "  - Dashboard service (8766) from anywhere"
echo "  - ICMP ping from anywhere"
echo ""
echo "To apply to your instance:"
echo "  exo compute instance add-security-group <instance-name> $SG_NAME --zone $ZONE"
