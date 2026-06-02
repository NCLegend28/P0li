#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Polymarket bot VPS bootstrap — Exoscale, Ubuntu 24.04 LTS
#
# Runs ON the VPS as root, on a fresh instance. Idempotent: safe to re-run.
#
# What it does:
#   1. Patches the box
#   2. Installs Docker + the compose plugin
#   3. Installs Tailscale (SSH mode — no public port 22 needed for daily ops)
#   4. Creates `botuser` (uid 10001), grants Docker access, no sudo password
#   5. Locks UFW: deny all inbound EXCEPT on the tailscale0 interface
#   6. Configures Docker log rotation so json-file logs don't fill the disk
#
# Optional env vars (set before running to skip interactive prompts):
#   TAILSCALE_AUTHKEY   pre-auth key from https://login.tailscale.com/admin/settings/keys
#                       if unset, you'll get a one-time URL to click on your laptop
#   SSH_PUBKEY          your ed25519 public key for botuser's authorized_keys
#                       (only needed if you ever want to ssh outside of tailscale)
#
# Geo note: the Polymarket API geoblocks by IP. Provision this VPS in AT-VIE-1
# or AT-VIE-2 (Austria). Switzerland (CH-*) is fully blocked, Germany (DE-*)
# is trading-restricted.
#
# Run:
#   curl -fsSL https://raw.githubusercontent.com/<you>/polymarket-bot/main/vps-setup.sh | sudo bash
# or, after git clone:
#   sudo TAILSCALE_AUTHKEY=tskey-auth-... ./vps-setup.sh
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi

BOTUSER="${BOTUSER:-botuser}"
BOT_DIR="/home/${BOTUSER}/polymarket-bot"

log() { printf '\n\033[1;36m[setup]\033[0m %s\n' "$*"; }

# ─── 1. Patch ────────────────────────────────────────────────────────────────
log "1/6 Updating base system"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq ca-certificates curl gnupg ufw fail2ban git logrotate

# ─── 2. Docker ───────────────────────────────────────────────────────────────
log "2/6 Installing Docker Engine + compose plugin"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi

# Cap Docker log size (json-file driver) so a chatty bot doesn't fill /var
log "  → configuring Docker log rotation (50MB × 5)"
mkdir -p /etc/docker
cat >/etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "5" }
}
JSON
systemctl restart docker

# ─── 3. Tailscale ────────────────────────────────────────────────────────────
log "3/6 Installing Tailscale"
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

# Bring the node up in SSH mode. With --ssh, tailscaled runs its own SSH
# server bound to the tailnet — no need to expose port 22 publicly.
if ! tailscale status >/dev/null 2>&1; then
  if [[ -n "${TAILSCALE_AUTHKEY:-}" ]]; then
    log "  → joining tailnet with provided authkey"
    tailscale up --ssh --authkey="${TAILSCALE_AUTHKEY}" \
      --hostname="$(hostname -s)-polybot"
  else
    log "  → opening tailnet auth URL — visit it on your laptop"
    tailscale up --ssh --hostname="$(hostname -s)-polybot"
  fi
fi

# ─── 4. botuser ──────────────────────────────────────────────────────────────
log "4/6 Creating ${BOTUSER}"
if ! id -u "${BOTUSER}" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" --uid 10001 "${BOTUSER}"
fi
usermod -aG docker "${BOTUSER}"

# Passwordless sudo for restart-the-bot ergonomics. Lock this down later if
# you ever add a second human to the box.
echo "${BOTUSER} ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-${BOTUSER}"
chmod 0440 "/etc/sudoers.d/90-${BOTUSER}"

# Install SSH key if provided (break-glass path; daily access is tailscale ssh)
if [[ -n "${SSH_PUBKEY:-}" ]]; then
  install -d -m 700 -o "${BOTUSER}" -g "${BOTUSER}" "/home/${BOTUSER}/.ssh"
  echo "${SSH_PUBKEY}" >> "/home/${BOTUSER}/.ssh/authorized_keys"
  chown "${BOTUSER}:${BOTUSER}" "/home/${BOTUSER}/.ssh/authorized_keys"
  chmod 600 "/home/${BOTUSER}/.ssh/authorized_keys"
fi

install -d -m 755 -o "${BOTUSER}" -g "${BOTUSER}" "${BOT_DIR}"

# ─── 5. Firewall ─────────────────────────────────────────────────────────────
log "5/6 Configuring UFW (tailscale0-only inbound)"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
# SSH only on the tailnet interface. Public port 22 stays closed. Break-glass
# path is the Exoscale web console (KVM-style serial).
ufw allow in on tailscale0 to any port 22 proto tcp comment 'ssh over tailnet'
# Allow all tailnet traffic (so the dashboard at 8766 is reachable via your
# laptop's tailscale-assigned IP when you want to peek)
ufw allow in on tailscale0 comment 'trust the tailnet'
ufw --force enable

# Belt and suspenders: harden public sshd. Even though UFW blocks it, if a
# Security Group misconfig opened port 22, this keeps password auth off.
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart ssh

# ─── 6. Bot data logrotate ───────────────────────────────────────────────────
log "6/6 Configuring logrotate for ${BOT_DIR}/data/trades/"
cat >/etc/logrotate.d/polybot <<EOF
${BOT_DIR}/data/trades/*.log
${BOT_DIR}/data/trades/*.jsonl {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    create 0644 ${BOTUSER} ${BOTUSER}
    copytruncate
}
EOF

# ─── Done ────────────────────────────────────────────────────────────────────
TS_IP="$(tailscale ip -4 2>/dev/null || echo 'pending')"
cat <<EOF

═══════════════════════════════════════════════════════════════════════════
  Bootstrap complete. Tailscale IP: ${TS_IP}

  From your laptop:
    tailscale ssh ${BOTUSER}@$(hostname -s)-polybot

  Next (see RUNBOOK.md → §3):
    cd ${BOT_DIR}
    git clone <your-repo-url> .
    cp .env.weather.example .env.weather   # then fill in secrets
    docker compose up -d
═══════════════════════════════════════════════════════════════════════════
EOF
