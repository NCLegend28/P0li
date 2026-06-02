# Polymarket Bot — Implementation Plan

> **Status as of March 2026:** Weather bot live in paper trading (+$77 profit, 74% win rate).
> Foundation infrastructure complete. Five additional bots planned across six phases.

---

## Table of Contents

1. [Current State](#current-state)
2. [Architecture Overview](#architecture-overview)
3. [Phase 1 — Crypto Bot](#phase-1--crypto-bot)
4. [Phase 2 — Sports Bot](#phase-2--sports-bot)
5. [Phase 3 — Live Execution](#phase-3--live-execution)
6. [Phase 4 — Politics Bot](#phase-4--politics-bot)
7. [Phase 5 — Multi-Page App](#phase-5--multi-page-app)
8. [Phase 6 — Arb Bot](#phase-6--arb-bot)
9. [Subscription Tiers](#subscription-tiers)
10. [Build Timeline](#build-timeline)
11. [File Reference](#file-reference)

---

## Current State

### Codebase

```
src/polybot/
├── api/
│   ├── gamma.py          159L   Polymarket market discovery
│   └── openmeteo.py      283L   Weather forecasts · 156 cities
├── backtest/
│   └── engine.py         517L   CLOB price history backtester
├── paper/
│   └── trader.py         224L   Paper trading engine · JSONL persistence
├── scanner/
│   ├── graph.py          235L   LangGraph 5-node pipeline
│   └── state.py           29L   Shared scan state
├── strategies/
│   ├── exit.py           158L   4-trigger exit engine
│   └── weather.py        239L   Normal CDF probability model
├── telegram/
│   └── bot.py            248L   Alerts + command handlers
├── ui/
│   └── dashboard.py      623L   Rich terminal TUI
├── web/
│   └── server.py         597L   FastAPI WebSocket dashboard
├── cli.py                322L   Entry point
└── config.py              38L   Pydantic settings
```

**Total: 3,925 lines · 24 files**

### Weather Bot Performance

| Metric | Value |
|--------|-------|
| Backtest win rate (30d) | 74% |
| Avg P&L per trade (bt) | +67.5% |
| Live paper profit | +$77.56 (+7.76%) |
| Live win rate | 47% (early — exit engine tuning in progress) |
| Active cities | 156 in coordinate database |
| Live weather markets | 55 · $138k 24h volume |
| Max open positions | 10 |
| Min edge threshold | 8% |

### Reusable Infrastructure (zero changes needed for new bots)

- **`PaperTrader`** — position management, JSONL persistence, P&L tracking
- **`ExitEngine`** — profit_target, edge_collapse, time_stop, market_closed triggers
- **`Market`, `Opportunity`, `PaperTrade` models** — generic across all market types
- **LangGraph pipeline** — `fetch_markets → filter_markets → fetch_forecasts → run_strategies → monitor_positions`; adding a new bot is one new `run_strategies` node
- **FastAPI WebSocket server** — add a route per bot type
- **TelegramAlerter** — plain text, works for any market category
- **Rich terminal dashboard** — panel-based, extend with new market panels

---

## Architecture Overview

```
DATA SOURCES          SHARED PIPELINE          STRATEGY NODES
─────────────         ────────────────         ──────────────
Gamma API    ──┐
Open-Meteo   ──┤──→  fetch_markets      ──→  WeatherBot   (live)
CLOB WS      ──┤──→  filter_markets     ──→  CryptoBot    (Phase 1)
CoinGecko    ──┤──→  fetch_forecasts    ──→  SportsBot    (Phase 2)
TheSportsDB  ──┤──→  run_strategies ────┤──→  PoliticsBot  (Phase 4)
Polls/GDELT  ──┘     monitor_positions  └──→  ArbBot       (Phase 6)
                           │
                    ┌──────┴──────┐
                    │             │
               PaperTrader   FastAPI WS
               ExitEngine    Dashboard
               Telegram      /weather
                             /crypto
                             /sports
                             /politics
                             /overview
```

Each new bot adds:
- One API client (`api/X.py`)
- One strategy module (`strategies/X.py`)
- One new node in `scanner/graph.py`
- One new dashboard page (`web/src/routes/X/+page.svelte`)

---

## Phase 1 — Crypto Bot

**Timeline:** ~1 week · No dependencies on other phases

**Market opportunity:** 46 active markets · $197k 24h volume · no API key required

### Model

Crypto price brackets use a log-normal distribution rather than the weather bot's normal CDF. The price at time T is modelled as:

```
P(T) ~ P(0) · exp(μT + σ√T · Z)    where Z ~ N(0,1)
```

For a bracket [L, H], the probability it resolves YES is:

```python
import math

def lognormal_bracket_prob(spot, lo, hi, sigma_daily, horizon_hours):
    """
    spot          current spot price
    lo, hi        bracket bounds
    sigma_daily   30-day rolling daily vol (e.g. 0.035 for 3.5%)
    horizon_hours hours until market closes
    """
    t = horizon_hours / 24
    sigma_t = sigma_daily * math.sqrt(t)
    mu_t = -0.5 * sigma_t ** 2  # risk-neutral drift

    def phi(x):
        return 1 / (1 + math.exp(-1.7 * max(-50, min(50, x))))

    d_lo = (math.log(lo / spot) - mu_t) / sigma_t if lo > 0 else -50
    d_hi = (math.log(hi / spot) - mu_t) / sigma_t if hi < 1e9 else 50

    return phi(d_hi) - phi(d_lo)
```

For "Up or Down" markets (binary on a 3-hour return), the model uses drift=0 (efficient market hypothesis) and σ from recent 15-minute returns, then computes P(close > open) from the log-normal CDF.

### New Files

| File | Lines (est.) | Description |
|------|-------------|-------------|
| `src/polybot/api/coingecko.py` | ~80 | Spot price + hourly OHLC from CoinGecko free tier |
| `src/polybot/strategies/crypto.py` | ~180 | Log-normal bracket + Up/Down model |

### Modified Files

| File | Change |
|------|--------|
| `src/polybot/scanner/graph.py` | Add `run_crypto_strategy` node |
| `src/polybot/config.py` | Add `crypto_min_edge`, `crypto_max_position_usd`, `crypto_edge_collapse` |

### Config (`.env`)

```env
CRYPTO_ENABLED=true
CRYPTO_MIN_EDGE=0.10          # 10% — tighter than weather due to vol spikes
CRYPTO_MAX_POSITION_USD=5     # Hard cap per position
CRYPTO_EDGE_COLLAPSE=0.02     # 2 cents vs 3 cents for weather
```

### Data Source

- **CoinGecko free tier** — no API key, 30 calls/min limit
- Endpoint: `GET /coins/{id}/market_chart?vs_currency=usd&days=1&interval=hourly`
- Supported assets: BTC, ETH, SOL, XRP, DOGE, BNB
- Cache TTL: 60s (prices move fast; don't cache longer)

### Risk Notes

- Crypto vol can spike 5× in minutes. The tighter `edge_collapse=0.02` (vs 0.03 for weather) is critical.
- "Up or Down" markets resolve in 3 hours — position sizing should reflect the fast binary outcome.
- Avoid entering positions within 30 minutes of major macro events (Fed announcements, CPI data).

---

## Phase 2 — Sports Bot

**Timeline:** ~2 weeks · No dependencies on other phases · **Start now — World Cup begins June 2026**

**Market opportunity:** 46 active markets · $407k 24h volume · FIFA WC markets $1–2M liquidity each

### Model

The Elo rating system assigns each team a numerical strength. Win probability between teams A and B is:

```python
def elo_win_prob(rating_a, rating_b, home_field_advantage=65):
    """
    home_field_advantage: ~65 Elo points for home games, 0 for neutral venue
    """
    return 1 / (1 + 10 ** ((rating_b - (rating_a + home_field_advantage)) / 400))
```

Edge is `|elo_win_prob - market_yes_price| > min_edge_threshold`.

For tournament markets ("Will X win the World Cup"), probability is the product of win probabilities across all likely bracket paths — computed via Monte Carlo simulation over the draw.

### Elo Database Bootstrap

538 publishes historical Elo ratings as public CSV files:
- `https://projects.fivethirtyeight.com/soccer-api/club/spi_global_rankings.csv` — club soccer
- `https://projects.fivethirtyeight.com/soccer-api/international/spi_global_rankings_intl.csv` — national teams
- NBA/NFL Elo: `https://projects.fivethirtyeight.com/nba-model/nba_elo.csv`

Bootstrap script: download CSVs → extract latest rating per team → save to `data/elo/nba.json`, `data/elo/soccer_intl.json`.

Update rule: after each game result, `K=20` for regular season, `K=10` for playoff/tournament games.

### New Files

| File | Lines (est.) | Description |
|------|-------------|-------------|
| `src/polybot/api/sportsdb.py` | ~120 | TheSportsDB free API client — schedules, results |
| `src/polybot/strategies/elo.py` | ~150 | Elo engine — ratings, updates, win probability |
| `src/polybot/strategies/sports.py` | ~200 | Market parser + edge detection |
| `data/elo/nba.json` | — | Bootstrapped from 538 CSV |
| `data/elo/soccer_intl.json` | — | FIFA World Cup national team ratings |
| `scripts/bootstrap_elo.py` | ~80 | One-time 538 CSV download + format |

### Modified Files

| File | Change |
|------|--------|
| `src/polybot/scanner/graph.py` | Add `run_sports_strategy` node |
| `src/polybot/config.py` | Add sports config block |

### Data Source

**TheSportsDB** (free, no API key):
- `GET /api/v1/json/3/eventsnextleague.php?id={league_id}` — upcoming fixtures
- `GET /api/v1/json/3/eventspastleague.php?id={league_id}` — past results for Elo updates
- League IDs: NBA=4387, NFL=4391, NHL=4380, EPL=4328
- FIFA World Cup group tables: manual entry + live updates from TheSportsDB

### FIFA World Cup Priority

The World Cup starts June 2026. Markets are already live at $1–2M liquidity per team. The edge comes from:

1. Small nations overpriced by hype (e.g. Qatar as host nation)
2. Favourites underpriced pre-tournament (Brazil, France, Argentina) based on historical Elo
3. Group stage upsets creating mispricing in knockout round markets

Building now gives time to validate the Elo model against pre-tournament qualifier results before real money is deployed.

### Config (`.env`)

```env
SPORTS_ENABLED=true
SPORTS_MIN_EDGE=0.08
SPORTS_MAX_POSITION_USD=15    # Higher than crypto — longer holds, more predictable
SPORTS_ELO_K_REGULAR=20
SPORTS_ELO_K_PLAYOFF=10
```

---

## Phase 3 — Live Execution

**Timeline:** ~1 week · Requires Phase 1 or Phase 2 validated in paper trading

**Prerequisites:** 100+ paper trades with positive expected value across at least one bot type

### Implementation

The switch from paper to live is a single flag in `.env`:

```env
LIVE_TRADING=false    # Change to true only after validation
POLYGON_WALLET=0x...
CLOB_API_KEY=...
CLOB_API_SECRET=...
MAX_DAILY_LOSS_USD=50  # Hard circuit breaker
```

`PaperTrader` gains a `live_mode` property. When `True`, after computing the paper trade it additionally submits a CLOB limit order via `py-clob-client`.

### New Files

| File | Lines (est.) | Description |
|------|-------------|-------------|
| `src/polybot/api/clob_client.py` | ~120 | `py-clob-client` wrapper — limit orders, fills, cancels |

### Modified Files

| File | Change |
|------|--------|
| `src/polybot/paper/trader.py` | Add `live_mode` flag, call `clob_client` on open/close |
| `src/polybot/config.py` | Wallet address, API keys, daily loss cap |

### Execution Rules

| Rule | Value | Rationale |
|------|-------|-----------|
| Order type | Limit only | Never market orders — avoid slippage on thin books |
| Orderbook check | Depth ≥ 3× position size | Prevent self-moving the price |
| Min position | $1 | CLOB minimum |
| Initial live cap | $50/day | Hard circuit breaker while validating |
| Ramp schedule | $50 → $200 → $500 → Kelly-sized | Increase only after 2 profitable weeks |
| Settlement | USDC on Polygon | Instant, no bank wire |

### Safety Circuit Breakers

```python
# In cli.py — checked before every open_position() call
if live_mode:
    daily_loss = sum(t.pnl_usd for t in today_closed if t.pnl_usd < 0)
    if abs(daily_loss) >= settings.max_daily_loss_usd:
        dash.log("Daily loss cap hit — live trading paused", "WARN")
        bot_state.paused = True
        return
```

---

## Phase 4 — Politics Bot

**Timeline:** ~3 weeks · Requires Phase 3 (live execution)

**Why live execution first:** Politics markets move on breaking news at 2AM. Paper trading cannot capture the exit timing that matters. The only safe approach is real execution with tight pre-news stop losses.

**Market opportunity:** 55 active markets · $1M+ 24h volume · most liquid category on Polymarket

### Model

Bayesian update: poll average as prior, GDELT news sentiment as likelihood update.

```python
def posterior_probability(poll_avg, sentiment_delta, sentiment_weight=0.15):
    """
    poll_avg         raw polling average (0–1)
    sentiment_delta  GDELT tone change vs 7d average (positive = improving)
    sentiment_weight how much to weight sentiment vs polls (conservative)
    """
    # Logit transform to work in log-odds space
    logit_prior = math.log(poll_avg / (1 - poll_avg))
    logit_update = logit_prior + sentiment_weight * sentiment_delta
    return 1 / (1 + math.exp(-logit_update))
```

### New Files

| File | Lines (est.) | Description |
|------|-------------|-------------|
| `src/polybot/api/polls.py` | ~120 | FiveThirtyEight CSV + PredictIt read-only API |
| `src/polybot/api/gdelt.py` | ~80 | GDELT 2.0 sentiment tone per entity |
| `src/polybot/strategies/politics.py` | ~220 | Bayesian model + market parser |

### Data Sources

**FiveThirtyEight** (free, GitHub CSV):
- `https://projects.fivethirtyeight.com/polls/data/president_polls.csv`
- Update frequency: when new polls are released (~daily)

**GDELT 2.0** (free, no key):
- `https://api.gdeltproject.org/api/v2/doc/doc?query={entity}&mode=tonechart`
- Returns sentiment tone time series for any named entity
- Update frequency: every 15 minutes
- Rate limit: 60 calls/hour on free tier — sufficient for weekly fetches per candidate

**PredictIt** (free read-only):
- `https://www.predictit.org/api/marketdata/all/`
- Useful as a second market reference to compare against Polymarket pricing

### Config (`.env`)

```env
POLITICS_ENABLED=true
POLITICS_MIN_EDGE=0.10         # Higher bar — adversarial market
POLITICS_MAX_POSITION_USD=10   # Lower than sports — black swan risk
POLITICS_MAX_BANKROLL_PCT=0.03 # Never more than 3% of bankroll per race
POLITICS_MIN_DAYS_TO_CLOSE=7   # Only trade markets resolving > 7 days out
```

### Risk Management

- **Down-ballot focus:** Major US presidential races have sophisticated participants with better data. Better edge exists in state-level races, international elections, and Fed decisions.
- **News event guard:** Do not hold positions within 48 hours of scheduled major news events (debates, major rulings, economic reports).
- **Black swan sizing:** Max 3% of total bankroll per political race, regardless of computed Kelly fraction.

---

## Phase 5 — Multi-Page App

**Timeline:** ~2 weeks · Can run in parallel with Phases 2–4

### Frontend Stack

```
Svelte 5 + Bun (replaces Node.js)
├── bun create svelte@latest web
├── bun install
├── bun run dev      # dev server
└── bun run build    # outputs to web/dist/ — served by FastAPI
```

Bun is a Node.js-compatible runtime that is ~3× faster. Drop-in replacement — same `npm` commands, same `package.json`.

### FastAPI Routing Upgrade

Current: single `/ws` WebSocket endpoint pushing all state.

Target: per-bot WebSocket routes with JWT gating.

```python
# web/server.py additions
@app.websocket("/ws/weather")
async def ws_weather(ws: WebSocket, token: str = Query(...)):
    await verify_jwt(token, required_tier="free")
    ...

@app.websocket("/ws/crypto")
async def ws_crypto(ws: WebSocket, token: str = Query(...)):
    await verify_jwt(token, required_tier="basic")
    ...

@app.websocket("/ws/overview")
async def ws_overview(ws: WebSocket, token: str = Query(...)):
    # Aggregate P&L + positions across all active bots
    await verify_jwt(token, required_tier="free")
    ...
```

### App Pages

| Route | Content | Tier |
|-------|---------|------|
| `/` | Overview — aggregate P&L, all positions, event log | Free |
| `/weather` | Weather bot dashboard (current terminal UI, Svelte port) | Free |
| `/crypto` | Crypto bot — prices, brackets, vol chart | Basic |
| `/sports` | Sports bot — Elo ratings, upcoming matches, positions | Pro |
| `/politics` | Politics bot — poll averages, sentiment feed | Pro |
| `/settings` | Bot config, position limits, tier management | All |

### New Files

| File | Description |
|------|-------------|
| `src/polybot/web/auth.py` | JWT middleware, tier validation |
| `web/` | Svelte project root |
| `web/src/routes/+layout.svelte` | Sidebar nav, bot P&L badges, tier gates |
| `web/src/routes/+page.svelte` | Overview page |
| `web/src/routes/weather/+page.svelte` | Weather bot page |
| `web/src/lib/stores.ts` | Reactive WebSocket state per bot |
| `web/src/lib/Tooltip.svelte` | Shared tooltip component |

### Svelte WebSocket Store (~10 lines)

```typescript
// web/src/lib/stores.ts
import { writable } from 'svelte/store';

export function createBotStore(route: string) {
  const store = writable(null);
  const token = localStorage.getItem('jwt');
  const ws = new WebSocket(`ws://localhost:8765/ws/${route}?token=${token}`);
  ws.onmessage = (e) => store.set(JSON.parse(e.data));
  ws.onclose = () => setTimeout(() => createBotStore(route), 2000);
  return store;
}

export const weatherState  = createBotStore('weather');
export const cryptoState   = createBotStore('crypto');
export const overviewState = createBotStore('overview');
```

---

## Phase 6 — Arb Bot

**Timeline:** ~3 weeks · Requires Phase 3 (live execution) + Phase 5 (CLOB WebSocket)

**Why last:** Arbitrage requires sub-second execution. Paper trading arb is meaningless. The CLOB WebSocket infrastructure from Phase 5 is the prerequisite.

### Arb Types

**Type 1 — Within-market pricing gap**
YES + NO prices should always sum to 1.00 (minus fees). When YES=0.52 and NO=0.51, sum=1.03 — buy the cheaper side. Rare but mechanical.

**Type 2 — Correlated market gap**
"Team A wins the championship" should be priced below "Team A wins the semifinal" (you can't win the championship without winning the semi). When the market inverts this relationship, buy the underpriced side.

**Type 3 — Bracket completeness**
Across all temperature brackets for one city one day, the probabilities of all YES outcomes should sum to ~1.00. When they don't — e.g. they sum to 0.85 — buy the underpriced ones to capture the gap.

### CLOB WebSocket Architecture

```
Startup:
  1× Gamma batch → collect clobTokenIds for all active markets

Runtime (persistent, zero polling):
  1× WebSocket connection → wss://ws-subscriptions-clob.polymarket.com/ws/market
  → Subscribe to N token IDs
  → Receive real-time price pushes
  → Run arb detector on every price update

On arb signal:
  → Submit limit order via py-clob-client
  → Monitor fill
  → Close position when gap closes
```

**API call reduction:** ~720 Gamma calls/day → ~10 calls/day (discovery only on startup + periodic refresh)

### New Files

| File | Lines (est.) | Description |
|------|-------------|-------------|
| `src/polybot/api/clob_ws.py` | ~150 | CLOB WebSocket client, subscription manager |
| `src/polybot/strategies/arb.py` | ~200 | Arb detector — all three types |

### Modified Files

| File | Change |
|------|--------|
| `src/polybot/scanner/graph.py` | Add streaming mode — replaces `fetch_markets` poll with WebSocket push |
| `src/polybot/cli.py` | Add `--streaming` flag to launch in WS mode |

### Config (`.env`)

```env
ARB_ENABLED=true
ARB_MIN_EDGE=0.03         # 3% after fees — lower bar than other bots
ARB_MAX_POSITION_USD=20   # Arb has near-zero directional risk
ARB_MAX_OPEN=20           # Can run more simultaneous arb positions
```

---

## Subscription Tiers

| Feature | Free | Basic ($29/mo) | Pro ($79/mo) | Alpha ($199/mo) |
|---------|------|----------------|--------------|-----------------|
| Bot slots | 1 | 2 | 4 | Unlimited |
| Weather bot | ✓ | ✓ | ✓ | ✓ |
| Crypto bot | — | ✓ | ✓ | ✓ |
| Sports bot | — | — | ✓ | ✓ |
| Politics bot | — | — | ✓ | ✓ |
| Arb bot | — | — | — | ✓ |
| Web dashboard | ✓ | ✓ | ✓ | ✓ |
| Telegram alerts | — | ✓ | ✓ | ✓ |
| Real execution | — | — | ✓ | ✓ |
| Backtester | — | 30d | 90d | Full |
| CLOB streaming | — | — | ✓ | ✓ |
| Multi-account | — | — | — | ✓ |
| API access | — | — | — | ✓ |

---

## Build Timeline

```
Week  1   2   3   4   5   6   7   8   9   10  11  12
      ─────────────────────────────────────────────────
Crypto bot
      ████░░
Sports bot
          ████████░░░░
Live execution
                  ████░░
Multi-page app
                      ████████░░░░
Politics bot
                          ████████████░░
Arb bot
                                  ██████████████
```

**█ = active development · ░ = validation/paper trading**

### Milestones

| Date | Milestone |
|------|-----------|
| Week 1 | Crypto bot paper trading live |
| Week 3 | Sports bot paper trading live — Elo model validated |
| Week 4 | Live execution enabled — $50/day cap |
| Week 5 | Multi-page Svelte app — `/weather` and `/crypto` pages live |
| Week 7 | Politics bot paper trading live |
| Week 8 | Live execution ramped to $200/day |
| Week 9 | CLOB WebSocket streaming replaces polling |
| Week 12 | Arb bot live — all five bots running |

---

## File Reference

### Complete File Tree (Phase 6 target state)

```
polymarket-bot/
├── .env
├── .env.example
├── pyproject.toml
├── data/
│   ├── trades/
│   │   └── paper_trades.jsonl
│   └── elo/
│       ├── nba.json              (Phase 2)
│       └── soccer_intl.json      (Phase 2)
├── scripts/
│   └── bootstrap_elo.py          (Phase 2)
├── web/                          (Phase 5 — Svelte app)
│   ├── package.json
│   ├── bun.lockb
│   └── src/
│       ├── routes/
│       │   ├── +layout.svelte
│       │   ├── +page.svelte        (overview)
│       │   ├── weather/
│       │   │   └── +page.svelte
│       │   ├── crypto/
│       │   │   └── +page.svelte
│       │   ├── sports/
│       │   │   └── +page.svelte
│       │   └── politics/
│       │       └── +page.svelte
│       └── lib/
│           ├── stores.ts
│           └── Tooltip.svelte
└── src/polybot/
    ├── api/
    │   ├── gamma.py              (exists)
    │   ├── openmeteo.py          (exists)
    │   ├── coingecko.py          (Phase 1)
    │   ├── sportsdb.py           (Phase 2)
    │   ├── clob_client.py        (Phase 3)
    │   ├── polls.py              (Phase 4)
    │   ├── gdelt.py              (Phase 4)
    │   └── clob_ws.py            (Phase 6)
    ├── strategies/
    │   ├── weather.py            (exists)
    │   ├── exit.py               (exists)
    │   ├── crypto.py             (Phase 1)
    │   ├── elo.py                (Phase 2)
    │   ├── sports.py             (Phase 2)
    │   ├── politics.py           (Phase 4)
    │   └── arb.py                (Phase 6)
    ├── web/
    │   ├── server.py             (exists — add routes Phase 5)
    │   └── auth.py               (Phase 5)
    ├── backtest/
    │   └── engine.py             (exists)
    ├── paper/
    │   └── trader.py             (exists — add live mode Phase 3)
    ├── scanner/
    │   ├── graph.py              (exists — add nodes each phase)
    │   └── state.py              (exists)
    ├── telegram/
    │   └── bot.py                (exists)
    ├── ui/
    │   └── dashboard.py          (exists)
    ├── cli.py                    (exists)
    └── config.py                 (exists — add blocks each phase)
```

---

*Last updated: March 2026*