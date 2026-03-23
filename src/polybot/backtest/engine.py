"""
Backtester v3 — real pre-resolution prices via CLOB history API.

Data pipeline:
  1. Gamma /markets (closed, updatedAt desc) → resolved weather markets
  2. CLOB /prices-history?startTs=...&endTs=... → price 1-7h before resolution
     (this is the price we would have seen when entering the trade)
  3. Open-Meteo forecast for the target date → model probability
  4. Simulate: enter at CLOB price, exit at resolution (0 or 1)
  5. Report: win rate, avg P&L, EV, Kelly fraction, city breakdown

Concurrency: all CLOB + Open-Meteo fetches run in asyncio.gather()
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta, date

import httpx
from loguru import logger
from rich.console import Console
from rich.table import Table
from rich import box

from polybot.api.openmeteo import OpenMeteoClient, CITY_COORDS
from polybot.models import Side
from polybot.strategies.weather import parse_question, estimate_probability

console = Console()

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"
WEATHER_KW = ["temperature", "°f", "°c", "degrees", "highest temp",
               "fahrenheit", "celsius", "lowest temp"]


# ─── Data structures ──────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    question:          str
    city:              str
    side:              Side
    entry_price:       float   # real CLOB price before resolution
    model_probability: float
    edge:              float
    resolution:        float   # 1.0 = YES resolved, 0.0 = NO resolved
    end_date:          str

    @property
    def exit_price(self) -> float:
        return self.resolution if self.side == Side.YES else (1.0 - self.resolution)

    @property
    def pnl_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price * 100

    @property
    def won(self) -> bool:
        return (self.side == Side.YES and self.resolution >= 0.99) or \
               (self.side == Side.NO  and self.resolution <= 0.01)


@dataclass
class BacktestResult:
    trades:             list[BacktestTrade] = field(default_factory=list)
    markets_scanned:    int   = 0
    markets_parseable:  int   = 0
    markets_with_edge:  int   = 0
    clob_hits:          int   = 0
    min_edge_threshold: float = 0.08
    days_back:          int   = 14

    @property
    def total(self) -> int:  return len(self.trades)

    @property
    def wins(self) -> int:   return sum(1 for t in self.trades if t.won)

    @property
    def win_rate(self) -> float:
        return self.wins / self.total if self.total else 0.0

    @property
    def avg_edge(self) -> float:
        return sum(t.edge for t in self.trades) / self.total if self.total else 0.0

    @property
    def avg_pnl_pct(self) -> float:
        return sum(t.pnl_pct for t in self.trades) / self.total if self.total else 0.0

    @property
    def total_pnl_pct(self) -> float:
        return sum(t.pnl_pct for t in self.trades)

    @property
    def expected_value(self) -> float:
        if not self.trades:
            return 0.0
        win_r  = [abs((t.exit_price - t.entry_price) / t.entry_price)
                  for t in self.trades if t.won    and t.entry_price > 0]
        loss_r = [abs((t.exit_price - t.entry_price) / t.entry_price)
                  for t in self.trades if not t.won and t.entry_price > 0]
        avg_w  = sum(win_r)  / len(win_r)  if win_r  else 0.0
        avg_l  = sum(loss_r) / len(loss_r) if loss_r else 0.0
        return self.win_rate * avg_w - (1 - self.win_rate) * avg_l

    @property
    def kelly_fraction(self) -> float:
        win_r  = [abs((t.exit_price - t.entry_price) / t.entry_price)
                  for t in self.trades if t.won    and t.entry_price > 0]
        loss_r = [abs((t.exit_price - t.entry_price) / t.entry_price)
                  for t in self.trades if not t.won and t.entry_price > 0]
        if not win_r or not loss_r:
            return 0.0
        avg_w = sum(win_r)  / len(win_r)
        avg_l = sum(loss_r) / len(loss_r)
        b     = avg_w / avg_l if avg_l else 0.0
        p, q  = self.win_rate, 1 - self.win_rate
        return max(0.0, (b * p - q) / b if b else 0.0)


# ─── Gamma: fetch resolved weather markets ────────────────────────────────────

async def _fetch_resolved_weather_raw(
    days_back: int = 14,
    max_pages: int = 8,
) -> list[dict]:
    cutoff  = datetime.now(timezone.utc) - timedelta(days=days_back)
    results = []

    async with httpx.AsyncClient(
        base_url = GAMMA_BASE,
        timeout  = 20.0,
        headers  = {"Accept": "application/json"},
    ) as client:
        for page in range(max_pages):
            resp = await client.get("/markets", params={
                "limit":     200,
                "closed":    "true",
                "order":     "updatedAt",
                "ascending": "false",
                "offset":    page * 200,
            })
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break

            # Stop paginating when oldest record in batch predates cutoff
            oldest_str = batch[-1].get("updatedAt", "")
            if oldest_str:
                try:
                    oldest_dt = datetime.fromisoformat(oldest_str.replace("Z", "+00:00"))
                    if oldest_dt < cutoff:
                        batch = [m for m in batch if _updated_after(m, cutoff)]
                        results.extend(_weather_filter(batch))
                        break
                except ValueError:
                    pass

            results.extend(_weather_filter(batch))

    logger.info(f"Fetched {len(results)} resolved weather markets (last {days_back}d)")
    return results


def _updated_after(raw: dict, cutoff: datetime) -> bool:
    s = raw.get("updatedAt", "")
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) >= cutoff
    except Exception:
        return False


def _weather_filter(batch: list[dict]) -> list[dict]:
    return [m for m in batch
            if any(kw in m.get("question", "").lower() for kw in WEATHER_KW)]


def _extract_resolution(raw: dict) -> float | None:
    try:
        prices = json.loads(raw.get("outcomePrices", ""))
        yes_p  = float(prices[0])
        if yes_p >= 0.99: return 1.0
        if yes_p <= 0.01: return 0.0
        return None
    except Exception:
        return None


def _end_timestamp(raw: dict) -> int | None:
    s = raw.get("endDate") or (raw.get("endDateIso", "") + "T12:00:00Z")
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


# ─── CLOB: pre-resolution price ───────────────────────────────────────────────

async def _clob_price_before_resolution(
    token:  str,
    end_ts: int,
    *,
    hours_before: int = 7,
    client: httpx.AsyncClient,
) -> float | None:
    """
    Fetch the CLOB price in the window [end_ts - hours_before, end_ts - 1h].
    Returns the last available price in that window, or None if no data.
    """
    start_ts = end_ts - hours_before * 3600
    stop_ts  = end_ts - 3600   # 1 hour before resolution

    try:
        resp = await client.get(
            f"{CLOB_BASE}/prices-history",
            params={"market": token, "startTs": start_ts, "endTs": stop_ts, "fidelity": 60},
            timeout=15.0,
        )
        if resp.status_code != 200:
            return None
        hist = resp.json().get("history", [])
        if not hist:
            return None
        # Return the price closest to resolution (last point in window)
        return float(hist[-1]["p"])
    except Exception as e:
        logger.debug(f"CLOB fetch error for {token[:20]}: {e}")
        return None


# ─── Core backtest ────────────────────────────────────────────────────────────

async def run_backtest(
    days_back: int   = 14,
    min_edge:  float = 0.08,
    verbose:   bool  = True,
) -> BacktestResult:

    result = BacktestResult(min_edge_threshold=min_edge, days_back=days_back)

    # 1. Resolved weather markets from Gamma
    raw_markets = await _fetch_resolved_weather_raw(days_back=days_back)
    result.markets_scanned = len(raw_markets)
    if not raw_markets:
        logger.warning("No resolved weather markets found.")
        return result

    # 2. Parse questions + build city list
    city_dates:  dict[str, str]  = {}
    valid_entries = []

    for raw in raw_markets:
        wq  = parse_question(raw.get("question", ""))
        res = _extract_resolution(raw)
        ts  = _end_timestamp(raw)
        if not (wq and res is not None and ts):
            continue

        result.markets_parseable += 1
        tokens_raw = raw.get("clobTokenIds", "[]")
        try:
            tokens = json.loads(tokens_raw)
        except Exception:
            tokens = []
        if not tokens:
            continue

        # YES token = index 0
        yes_token = tokens[0]
        valid_entries.append((raw, wq, res, ts, yes_token))
        if wq.city not in city_dates:
            city_dates[wq.city] = wq.target_date

    logger.info(f"Valid entries: {len(valid_entries)}/{result.markets_scanned}  "
                f"Cities: {list(city_dates.keys())[:8]}...")

    if not valid_entries:
        return result

    # 3. Fetch Open-Meteo forecasts + CLOB prices concurrently
    known_cities = {c: td for c, td in city_dates.items() if c in CITY_COORDS}
    unknown      = set(city_dates) - set(known_cities)
    if unknown:
        logger.warning(f"Unknown cities (no coords): {unknown}")

    # Semaphore limits concurrent Open-Meteo calls to 5 to avoid 429s
    _sem = asyncio.Semaphore(5)

    async def _fetch_fc(city, td):
        async with _sem:
            await asyncio.sleep(0.1)   # 100ms spacing between batches
            async with OpenMeteoClient() as m:
                return city, await m.fetch_forecast(city, target_date=td)

    # Fetch forecasts with rate-limiting
    fc_tasks    = [_fetch_fc(c, td) for c, td in known_cities.items()]
    fc_results  = await asyncio.gather(*fc_tasks, return_exceptions=True)
    forecasts: dict[str, object] = {}
    for r in fc_results:
        if isinstance(r, Exception):
            logger.warning(f"Forecast error: {r}")
        else:
            city, fc = r
            forecasts[city] = fc

    logger.info(f"Forecasts: {len(forecasts)}/{len(known_cities)}")

    # Fetch all CLOB prices concurrently
    async with httpx.AsyncClient(timeout=20.0) as clob_client:
        clob_tasks = [
            _clob_price_before_resolution(token, ts, client=clob_client)
            for _, _, _, ts, token in valid_entries
        ]
        clob_prices = await asyncio.gather(*clob_tasks, return_exceptions=True)

    result.clob_hits = sum(1 for p in clob_prices
                           if isinstance(p, float) and p is not None)
    logger.info(f"CLOB prices retrieved: {result.clob_hits}/{len(valid_entries)}")

    # 4. Simulate trades
    for (raw, wq, resolution, ts, token), clob_price in zip(valid_entries, clob_prices):
        forecast = forecasts.get(wq.city)
        if not forecast:
            continue

        if isinstance(clob_price, Exception) or clob_price is None:
            continue

        yes_mkt = float(clob_price)

        # Skip if price was already resolved at detection time
        if yes_mkt >= 0.95 or yes_mkt <= 0.05:
            continue

        model_prob = estimate_probability(wq, forecast)
        edge       = model_prob - yes_mkt

        if abs(edge) < min_edge:
            continue

        result.markets_with_edge += 1
        side        = Side.YES if edge > 0 else Side.NO
        entry_price = yes_mkt if side == Side.YES else round(1 - yes_mkt, 4)

        trade = BacktestTrade(
            question          = raw.get("question", ""),
            city              = wq.city,
            side              = side,
            entry_price       = entry_price,
            model_probability = model_prob,
            edge              = abs(edge),
            resolution        = resolution,
            end_date          = raw.get("endDateIso", ""),
        )
        result.trades.append(trade)

        if verbose:
            sym = "✓" if trade.won else "✗"
            logger.info(
                f"  {sym} {side.value:3} '{raw.get('question','')[:50]}' | "
                f"entry={entry_price:.3f} model={model_prob:.1%} "
                f"edge=+{abs(edge):.1%} → exit={trade.exit_price:.0f} "
                f"pnl={trade.pnl_pct:+.1f}%"
            )

    return result


# ─── Report ───────────────────────────────────────────────────────────────────

def print_report(result: BacktestResult) -> None:
    console.rule(
        f"[bold cyan]📊 Backtest  last {result.days_back}d  "
        f"min_edge={result.min_edge_threshold:.0%}  "
        f"CLOB hits={result.clob_hits}/{result.markets_parseable}"
    )

    g = Table.grid(padding=(0, 2))
    g.add_column(style="grey50")
    g.add_column(justify="right")

    def row(lbl, val, col="white"):
        g.add_row(lbl, f"[{col}]{val}[/]")

    row("Markets scanned",    str(result.markets_scanned))
    row("Parseable",          str(result.markets_parseable))
    row("CLOB prices found",  str(result.clob_hits))
    row("Trades simulated",   str(result.total))
    g.add_row("", "")
    wc = "bright_green" if result.win_rate >= 0.55 else "bright_red"
    row("Win rate",
        f"{result.win_rate:.1%}  ({result.wins}W / {result.total - result.wins}L)", wc)
    row("Avg edge at entry",  f"+{result.avg_edge:.1%}", "bright_cyan")
    pc = "bright_green" if result.avg_pnl_pct >= 0 else "bright_red"
    row("Avg P&L / trade",    f"{result.avg_pnl_pct:+.1f}%", pc)
    row("Total P&L (equal $)", f"{result.total_pnl_pct:+.1f}%", pc)
    ec = "bright_green" if result.expected_value >= 0 else "bright_red"
    row("Expected value",     f"{result.expected_value:+.4f}  (per $ staked)", ec)
    kc = "bright_yellow" if result.kelly_fraction > 0 else "grey50"
    row("Kelly fraction",     f"{result.kelly_fraction:.1%}  ← max % of bankroll per trade", kc)
    console.print(g)

    if not result.trades:
        console.print("\n[yellow]No trades found — try reducing min_edge or increasing days_back.[/]")
        console.rule()
        return

    console.print()

    t = Table(box=box.SIMPLE, show_header=True,
              header_style="bold bright_cyan", expand=True, padding=(0, 1))
    t.add_column("DATE",  width=11, style="grey50", no_wrap=True)
    t.add_column("CITY",  width=13, style="bright_cyan", no_wrap=True)
    t.add_column("SIDE",  width=5,  justify="center")
    t.add_column("ENTRY", width=7,  justify="right")
    t.add_column("MODEL", width=7,  justify="right")
    t.add_column("EDGE",  width=7,  justify="right")
    t.add_column("EXIT",  width=5,  justify="right")
    t.add_column("P&L",   width=9,  justify="right")
    t.add_column("",      width=2,  justify="center")

    for tr in sorted(result.trades, key=lambda x: x.end_date, reverse=True):
        sc  = "bright_green" if tr.side == Side.YES else "bright_red"
        pc  = "bright_green" if tr.pnl_pct >= 0 else "bright_red"
        sym = "[bright_green]✓[/]" if tr.won else "[bright_red]✗[/]"
        res = "YES" if tr.resolution >= 0.99 else "NO"
        rc  = "bright_green" if tr.resolution >= 0.99 else "bright_red"
        t.add_row(
            tr.end_date,
            tr.city,
            f"[{sc}]{tr.side.value}[/]",
            f"[grey50]{tr.entry_price:.3f}[/]",
            f"[white]{tr.model_probability:.1%}[/]",
            f"[bright_cyan]+{tr.edge:.1%}[/]",
            f"[{rc}]{res}[/]",
            f"[{pc}]{tr.pnl_pct:+.1f}%[/]",
            sym,
        )
    console.print(t)

    # City breakdown
    city_stats: dict[str, dict] = {}
    for tr in result.trades:
        s = city_stats.setdefault(tr.city, {"n": 0, "w": 0, "pnl": 0.0})
        s["n"] += 1
        s["w"] += int(tr.won)
        s["pnl"] += tr.pnl_pct

    if len(city_stats) > 1:
        console.print()
        ct = Table(box=box.SIMPLE, show_header=True,
                   header_style="bold grey50", padding=(0, 2),
                   title="By City")
        ct.add_column("CITY",   style="bright_cyan")
        ct.add_column("TRADES", justify="right")
        ct.add_column("WIN%",   justify="right")
        ct.add_column("P&L%",   justify="right")

        for city, s in sorted(city_stats.items(), key=lambda x: -x[1]["pnl"]):
            wr  = s["w"] / s["n"]
            wc  = "bright_green" if wr >= 0.55 else "bright_red"
            pc  = "bright_green" if s["pnl"] >= 0 else "bright_red"
            ct.add_row(
                city,
                str(s["n"]),
                f"[{wc}]{wr:.0%}[/]",
                f"[{pc}]{s['pnl']:+.1f}%[/]",
            )
        console.print(ct)

    console.rule()


# ─── CLI ─────────────────────────────────────────────────────────────────────

async def main(days_back: int = 14, min_edge: float = 0.08) -> None:
    from loguru import logger as _log
    _log.remove()
    _log.add(
        lambda m: console.print(f"[grey50]{m.strip()}[/]"),
        level    = "INFO",
        format   = "{time:HH:mm:ss} | {message}",
        colorize = False,
    )
    console.print(
        f"\n[bold cyan]🔍 Backtesting weather strategy — "
        f"last {days_back} days  min_edge={min_edge:.0%}[/]\n"
    )
    result = await run_backtest(days_back=days_back, min_edge=min_edge)
    print_report(result)


if __name__ == "__main__":
    import sys
    days = int(sys.argv[1])   if len(sys.argv) > 1 else 14
    edge = float(sys.argv[2]) if len(sys.argv) > 2 else 0.08
    asyncio.run(main(days_back=days, min_edge=edge))