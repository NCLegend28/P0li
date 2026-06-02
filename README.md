# 🦞 Polymarket Paper Bot

Automated paper-trading scanner for Polymarket weather markets.
Detects probability mispricing using Open-Meteo forecast data, runs entirely
on free APIs, and pushes live alerts via Telegram.

---

## Architecture

```
Gamma API (market discovery)
    ↓
LangGraph Pipeline (5 nodes)
    fetch_markets → filter_markets → fetch_forecasts
        → run_strategies → monitor_positions
    ↓                           ↓
PaperTrader                ExitEngine
(open positions)           (close positions)
    ↓
JSONL trade log + Rich dashboard + Telegram alerts
```

---

## Quick Start (local dev)

```bash
# 1. Clone / unzip project
cd polymarket-bot

# 2. Install deps
pip install -e ".[dev]"
# or: pip install langgraph langchain-core httpx pydantic pydantic-settings \
#         loguru rich python-dotenv python-telegram-bot

# 3. Configure
cp .env.example .env
# Edit .env — at minimum no changes needed for paper trading
# Add TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to get alerts

# 4. Run
PYTHONPATH=src python -m polybot.cli
```

---

## Telegram Setup

1. Open Telegram → search `@BotFather` → `/newbot`
2. Copy the token into `.env` as `TELEGRAM_BOT_TOKEN`
3. Message your bot once, then visit:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   Copy `"id"` from the `"chat"` object → `TELEGRAM_CHAT_ID`

Available commands:
| Command | Action |
|---|---|
| `/status` | Scanner state, balance, P&L |
| `/positions` | All open trades |
| `/pnl` | Closed trade summary |
| `/pause` | Pause scan loop |
| `/resume` | Resume scan loop |
| `/stop` | Graceful shutdown |

---

## Hetzner VPS Deployment

```bash
# On Hetzner CX22 (Ubuntu 24.04)

# 1. Provision server
# Choose: CX22, Ubuntu 24.04, add your SSH key

# 2. Harden
ssh root@<IP>
adduser botuser
usermod -aG sudo botuser
ufw allow OpenSSH
ufw allow 22/tcp
ufw enable
# Disable password auth: edit /etc/ssh/sshd_config → PasswordAuthentication no

# 3. Install Python
apt update && apt install -y python3.12 python3-pip git

# 4. Deploy bot
su - botuser
git clone <your-repo> polymarket-bot
cd polymarket-bot
pip install langgraph langchain-core httpx pydantic pydantic-settings \
    loguru rich python-dotenv python-telegram-bot --break-system-packages
cp .env.example .env
# Edit .env with real values (use Doppler in prod — see below)

# 5. Run as systemd service
sudo tee /etc/systemd/system/polybot.service << EOF
[Unit]
Description=Polymarket Paper Bot
After=network.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/home/botuser/polymarket-bot
Environment=PYTHONPATH=src
ExecStart=/usr/bin/python3 -m polybot.cli
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable polybot
sudo systemctl start polybot
sudo journalctl -u polybot -f   # tail logs
```

---

## Doppler (production secrets)

```bash
# Install Doppler CLI
curl -Ls https://cli.doppler.com/install.sh | sh

# Login + link project
doppler login
doppler setup

# Run with secrets injected automatically
doppler run -- python3 -m polybot.cli
```

Never put real private keys in `.env` files on the server.
Use Doppler to inject them at runtime — they never touch the filesystem.

---

## Key `.env` Settings

| Variable | Default | Notes |
|---|---|---|
| `SCAN_INTERVAL_SECONDS` | `120` | 2-minute scan cycle |
| `MIN_LIQUIDITY_USD` | `500` | Skip illiquid markets |
| `MIN_EDGE_THRESHOLD` | `0.08` | 8% minimum model vs market divergence |
| `PAPER_STARTING_BALANCE` | `1000` | Virtual dollars |
| `PAPER_MAX_POSITION_USD` | `10` | Max per trade |
| `MAX_OPEN_POSITIONS` | `5` | Circuit breaker |

---

## Project Structure

```
src/polybot/
├── config.py               # Pydantic settings → .env
├── models.py               # Market, Opportunity, PaperTrade
├── cli.py                  # Main entry point + scan loop
├── api/
│   ├── gamma.py            # Polymarket market discovery
│   └── openmeteo.py        # Free global weather forecasts
├── scanner/
│   ├── state.py            # LangGraph ScanState
│   └── graph.py            # 5-node pipeline
├── strategies/
│   ├── weather.py          # Probability model + question parser
│   └── exit.py             # Exit signal engine
├── paper/
│   └── trader.py           # Virtual position manager
└── telegram/
    └── bot.py              # Command handlers + alerter
```

---

## Paper Trading → Real Trading Checklist

Before going live, confirm all of these:

- [ ] 2+ weeks of paper trading with positive expected value
- [ ] Win rate > 55% on closed trades
- [ ] No bugs in exit logic (trades close as expected)
- [ ] Wallet private key stored in Doppler, never in `.env`
- [ ] Hot wallet holds ≤ $100 USDC to start
- [ ] Daily loss limit implemented and tested
- [ ] Telegram alerts working for every open/close
- [ ] Systemd service auto-restarts on crash
- [ ] VPS SSH hardened (key-only, fail2ban, non-standard port)

---

## Disclaimer

This is a research and educational project. Prediction markets are high-risk.
You can lose your entire deposit. Only trade with money you can afford to lose.
Not financial advice.
