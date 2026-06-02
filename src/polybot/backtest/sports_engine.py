"""
Sports backtester — validates the global Polymarket signal used for cross-platform arb.

Data pipeline:
  1. Gamma /markets (closed, updatedAt desc) → resolved sports markets
  2. CLOB /prices-history?startTs=...&endTs=... → global price before resolution
     (this is the Layer 1 signal we would have acted on)
  3. Simulate: enter when |clob_price - 0.5| > min_edge (strong directional signal)
  4. Optional: Odds API historical odds as a fair-value benchmark (requires key)
  5. Report: win rate, avg P&L, EV, Kelly, sport breakdown

Why this is useful:
  The live strategy trades when global_price differs from US_price.
  If global Polymarket is a good predictor of outcomes, those discrepancies
  are exploitable. This backtest measures global CLOB calibration directly.

Concurrency: all CLOB fetches run via asyncio.gather()
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import httpx
from loguru import logger
from rich.console import Console
from rich.table import Table
from rich import box

from polybot.models import Side

console = Console()

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"


# ─── Sport detection ─────────────────────────────────────────────────────────

_NBA_KW   = ("nba", "lakers", "celtics", "warriors", "bulls", "heat", "nets",
             "knicks", "bucks", "sixers", "suns", "nuggets", "clippers",
             "raptors", "mavericks", "mavs", "rockets", "spurs", "thunder",
             "blazers", "jazz", "kings", "pistons", "cavs", "hawks", "hornets",
             "magic", "pacers", "grizzlies", "pelicans", "wizards", "wolves")
_NFL_KW   = ("nfl", "super bowl", "patriots", "chiefs", "cowboys", "packers",
             "eagles", "steelers", "ravens", "49ers", "broncos", "seahawks",
             "rams", "chargers", "raiders", "dolphins", "bills", "colts",
             "texans", "jaguars", "titans", "bengals", "browns", "giants",
             "commanders", "falcons", "panthers", "saints", "buccaneers",
             "cardinals", "bears", "lions", "vikings")
_MLB_KW   = ("mlb", "world series", "yankees", "red sox", "dodgers", "cubs",
             "mets", "braves", "astros", "padres", "phillies")
_NHL_KW   = ("nhl", "stanley cup", "maple leafs", "bruins", "rangers",
             "blackhawks", "penguins", "flyers", "capitals", "avalanche",
             "oilers", "canucks", "canadiens")
_SOCCER_KW = ("premier league", "champions league", "epl", "ucl", "la liga",
              "bundesliga", "serie a", "liverpool", "manchester", "arsenal",
              "chelsea", "barcelona", "real madrid", "bayern", "psg")
_SPORTS_KW = (
    "nba", "nfl", "mlb", "nhl", "mls", "epl", "ufc", "wnba",
    "premier league", "champions league", "la liga", "serie a",
    "bundesliga", "ligue 1", "ncaa", "march madness",
    " game ", " match ", " series ", "playoff", "championship",
    "super bowl", "world series", "stanley cup", "nba finals",
    "world cup", "euro ", " cup ",
    "lakers", "celtics", "warriors", "bulls", "heat", "nets",
    "knicks", "bucks", "sixers", "suns", "nuggets", "clippers",
    "raptors", "mavs", "mavericks", "rockets", "spurs", "thunder",
    "blazers", "jazz", "kings", "pistons", "cavs", "cavaliers",
    "hawks", "hornets", "magic", "pacers", "grizzlies", "pelicans",
    "wizards", "timberwolves", "wolves",
    "patriots", "chiefs", "cowboys", "packers", "eagles", "steelers",
    "ravens", "niners", "49ers", "broncos", "seahawks", "rams",
    "chargers", "raiders", "dolphins", "bills", "colts", "texans",
    "jaguars", "titans", "bengals", "browns", "giants", "commanders",
    "falcons", "panthers", "saints", "buccaneers", "bears", "lions", "vikings",
    "yankees", "red sox", "dodgers", "cubs", "mets", "braves", "astros",
    "maple leafs", "canadiens", "bruins", "blackhawks", "penguins",
    "liverpool", "manchester", "arsenal", "chelsea", "barcelona",
    "real madrid", "bayern", "psg",
    " win the ", "beat the ", "cover the spread",
)


def _detect_sport(question: str) -> str:
    q = question.lower()
    if any(kw in q for kw in _NBA_KW):
        return "NBA"
    if any(kw in q for kw in _NFL_KW):
        return "NFL"
    if any(kw in q for kw in _MLB_KW):
        return "MLB"
    if any(kw in q for kw in _NHL_KW):
        return "NHL"
    if any(kw in q for kw in _SOCCER_KW):
        return "Soccer"
    return "Other"


def _is_sports(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in _SPORTS_KW)


# ─── Data structures ──────────────────────────────────────────────────────────

@dataclass
class SportsBacktestTrade:
    question:    str
    sport:       str
    side:        Side
    entry_price: float   # global CLOB price before resolution
    signal:      float   # price deviation from 0.5 (our edge proxy)
    resolution:  float   # 1.0 = YES resolved, 0.0 = NO resolved
    end_date:    str

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
class SportsBacktestResult:
    trades:              list[SportsBacktestTrade] = field(default_factory=list)
    markets_scanned:     int   = 0
    markets_with_odds:   int   = 0
    clob_hits:           int   = 0
    min_edge_threshold:  float = 0.05
    days_back:           int   = 30
    hours_before:        int   = 6

    @property
    def total(self) -> int:  return len(self.trades)

    @property
    def wins(self) -> int:   return sum(1 for t in self.trades if t.won)

    @property
    def win_rate(self) -> float:
        return self.wins / self.total if self.total else 0.0

    @property
    def avg_signal(self) -> float:
        return sum(t.signal for t in self.trades) / self.total if self.total else 0.0

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
                  for t in self.trades if t.won     and t.entry_price > 0]
        loss_r = [abs((t.exit_price - t.entry_price) / t.entry_price)
                  for t in self.trades if not t.won and t.entry_price > 0]
        avg_w  = sum(win_r)  / len(win_r)  if win_r  else 0.0
        avg_l  = sum(loss_r) / len(loss_r) if loss_r else 0.0
        return self.win_rate * avg_w - (1 - self.win_rate) * avg_l

    @property
    def kelly_fraction(self) -> float:
        win_r  = [abs((t.exit_price - t.entry_price) / t.entry_price)
                  for t in self.trades if t.won     and t.entry_price > 0]
        loss_r = [abs((t.exit_price - t.entry_price) / t.entry_price)
                  for t in self.trades if not t.won and t.entry_price > 0]
        if not win_r or not loss_r:
            return 0.0
        avg_w = sum(win_r)  / len(win_r)
        avg_l = sum(loss_r) / len(loss_r)
        b     = avg_w / avg_l if avg_l else 0.0
        p, q  = self.win_rate, 1 - self.win_rate
        return max(0.0, (b * p - q) / b if b else 0.0)

    def by_sport(self) -> dict[str, dict]:
        stats: dict[str, dict] = {}
        for t in self.trades:
            s = stats.setdefault(t.sport, {"n": 0, "w": 0, "pnl": 0.0})
            s["n"] += 1
            s["w"] += int(t.won)
            s["pnl"] += t.pnl_pct
        return stats


# ─── Gamma: fetch resolved sports markets ────────────────────────────────────

async def _fetch_resolved_sports_raw(
    days_back: int = 30,
    max_pages: int = 15,
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

            oldest_str = batch[-1].get("updatedAt", "")
            if oldest_str:
                try:
                    oldest_dt = datetime.fromisoformat(oldest_str.replace("Z", "+00:00"))
                    if oldest_dt < cutoff:
                        batch = [m for m in batch if _updated_after(m, cutoff)]
                        results.extend(_sports_filter(batch))
                        break
                except ValueError:
                    pass

            results.extend(_sports_filter(batch))

    logger.info(f"Fetched {len(results)} resolved sports markets (last {days_back}d)")
    return results


def _updated_after(raw: dict, cutoff: datetime) -> bool:
    s = raw.get("updatedAt", "")
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) >= cutoff
    except Exception:
        return False


def _sports_filter(batch: list[dict]) -> list[dict]:
    return [m for m in batch if _is_sports(m.get("question", ""))]


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
    s = raw.get("endDate") or (raw.get("endDateIso", "") + "T23:59:59Z")
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


# ─── CLOB: pre-resolution price ───────────────────────────────────────────────

async def _clob_price_before_resolution(
    token:  str,
    end_ts: int,
    *,
    hours_before: int = 6,
    client: httpx.AsyncClient,
) -> float | None:
    """
    Fetch the global CLOB price in the window [end_ts - hours_before, end_ts - 1h].
    This simulates what we would have seen when deciding to trade.
    """
    start_ts = end_ts - hours_before * 3600
    stop_ts  = end_ts - 3600  # 1h before resolution

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
        return float(hist[-1]["p"])
    except Exception as e:
        logger.debug(f"CLOB fetch error for {token[:20]}: {e}")
        return None


# ─── Core backtest ────────────────────────────────────────────────────────────

async def run_sports_backtest(
    days_back:    int   = 30,
    min_edge:     float = 0.05,
    hours_before: int   = 6,
    verbose:      bool  = True,
) -> SportsBacktestResult:
    """
    Run the sports backtest.

    Fetches resolved sports markets from Gamma, replays CLOB prices at
    entry time, and simulates trades whenever the global price deviated
    from 0.5 by at least min_edge.

    Args:
        days_back:    How many days of history to pull.
        min_edge:     Minimum |clob - 0.5| to simulate a trade (mirrors live threshold).
        hours_before: How many hours before resolution to simulate entry.
        verbose:      Log each individual trade.
    """
    result = SportsBacktestResult(
        min_edge_threshold=min_edge,
        days_back=days_back,
        hours_before=hours_before,
    )

    # 1. Resolved sports markets
    raw_markets = await _fetch_resolved_sports_raw(days_back=days_back)
    result.markets_scanned = len(raw_markets)
    if not raw_markets:
        logger.warning("No resolved sports markets found.")
        return result

    # 2. Parse + filter
    valid_entries = []
    for raw in raw_markets:
        res = _extract_resolution(raw)
        ts  = _end_timestamp(raw)
        if res is None or ts is None:
            continue

        tokens_raw = raw.get("clobTokenIds", "[]")
        try:
            tokens = json.loads(tokens_raw)
        except Exception:
            tokens = []
        if not tokens:
            continue

        valid_entries.append((raw, res, ts, tokens[0]))  # YES token = index 0

    logger.info(f"Valid entries (resolved + tokenized): {len(valid_entries)}/{result.markets_scanned}")
    if not valid_entries:
        return result

    # 3. Fetch CLOB prices concurrently
    async with httpx.AsyncClient(timeout=20.0) as clob_client:
        clob_tasks = [
            _clob_price_before_resolution(
                token, ts, hours_before=hours_before, client=clob_client
            )
            for _, _, ts, token in valid_entries
        ]
        clob_prices = await asyncio.gather(*clob_tasks, return_exceptions=True)

    result.clob_hits = sum(
        1 for p in clob_prices
        if isinstance(p, float) and p is not None
    )
    logger.info(f"CLOB prices retrieved: {result.clob_hits}/{len(valid_entries)}")

    # 4. Simulate trades
    for (raw, resolution, _ts, _token), clob_price in zip(valid_entries, clob_prices):
        if isinstance(clob_price, Exception) or clob_price is None:
            continue

        clob = float(clob_price)

        # Skip if market was already essentially resolved at entry time
        if clob >= 0.95 or clob <= 0.05:
            continue

        # Signal = deviation from 0.5; must exceed threshold
        deviation = clob - 0.5
        if abs(deviation) < min_edge:
            continue

        # Trade direction follows the global signal
        side        = Side.YES if deviation > 0 else Side.NO
        entry_price = clob if side == Side.YES else round(1.0 - clob, 4)
        signal      = abs(deviation)
        sport       = _detect_sport(raw.get("question", ""))

        trade = SportsBacktestTrade(
            question    = raw.get("question", ""),
            sport       = sport,
            side        = side,
            entry_price = entry_price,
            signal      = signal,
            resolution  = resolution,
            end_date    = raw.get("endDateIso", ""),
        )
        result.trades.append(trade)

        if verbose:
            sym = "✓" if trade.won else "✗"
            logger.info(
                f"  {sym} [{sport:5}] {side.value:3} "
                f"'{raw.get('question','')[:52]}' | "
                f"entry={entry_price:.3f} signal=+{signal:.1%} "
                f"→ exit={trade.exit_price:.0f} pnl={trade.pnl_pct:+.1f}%"
            )

    return result


# ─── Report ───────────────────────────────────────────────────────────────────

def print_sports_report(result: SportsBacktestResult) -> None:
    console.rule(
        f"[bold cyan]🏆 Sports Backtest  last {result.days_back}d  "
        f"min_edge={result.min_edge_threshold:.0%}  "
        f"entry={result.hours_before}h before  "
        f"CLOB hits={result.clob_hits}/{result.markets_scanned}"
    )

    g = Table.grid(padding=(0, 2))
    g.add_column(style="grey50")
    g.add_column(justify="right")

    def row(lbl, val, col="white"):
        g.add_row(lbl, f"[{col}]{val}[/]")

    row("Markets scanned",    str(result.markets_scanned))
    row("CLOB prices found",  str(result.clob_hits))
    row("Trades simulated",   str(result.total))
    g.add_row("", "")

    wc = "bright_green" if result.win_rate >= 0.55 else "bright_red"
    row("Win rate",
        f"{result.win_rate:.1%}  ({result.wins}W / {result.total - result.wins}L)", wc)
    row("Avg signal at entry", f"+{result.avg_signal:.1%}", "bright_cyan")
    pc = "bright_green" if result.avg_pnl_pct >= 0 else "bright_red"
    row("Avg P&L / trade",    f"{result.avg_pnl_pct:+.1f}%", pc)
    row("Total P&L (equal $)", f"{result.total_pnl_pct:+.1f}%", pc)
    ec = "bright_green" if result.expected_value >= 0 else "bright_red"
    row("Expected value",     f"{result.expected_value:+.4f}  (per $ staked)", ec)
    kc = "bright_yellow" if result.kelly_fraction > 0 else "grey50"
    row("Kelly fraction",     f"{result.kelly_fraction:.1%}  ← max % of bankroll per trade", kc)
    console.print(g)

    if not result.trades:
        console.print(
            "\n[yellow]No trades found — try reducing min_edge or increasing days_back.[/]"
        )
        console.rule()
        return

    console.print()

    t = Table(
        box=box.SIMPLE, show_header=True,
        header_style="bold bright_cyan", expand=True, padding=(0, 1),
    )
    t.add_column("DATE",   width=11, style="grey50",      no_wrap=True)
    t.add_column("SPORT",  width=7,  style="bright_cyan", no_wrap=True)
    t.add_column("SIDE",   width=5,  justify="center")
    t.add_column("ENTRY",  width=7,  justify="right")
    t.add_column("SIGNAL", width=8,  justify="right")
    t.add_column("EXIT",   width=5,  justify="right")
    t.add_column("P&L",    width=9,  justify="right")
    t.add_column("",       width=2,  justify="center")
    t.add_column("QUESTION", style="grey50")

    for tr in sorted(result.trades, key=lambda x: x.end_date, reverse=True):
        sc  = "bright_green" if tr.side == Side.YES else "bright_red"
        pc  = "bright_green" if tr.pnl_pct >= 0     else "bright_red"
        sym = "[bright_green]✓[/]" if tr.won else "[bright_red]✗[/]"
        res = "YES" if tr.resolution >= 0.99 else "NO"
        rc  = "bright_green" if tr.resolution >= 0.99 else "bright_red"
        # Truncate question for the table
        q   = tr.question[:55] + "…" if len(tr.question) > 55 else tr.question
        t.add_row(
            tr.end_date,
            tr.sport,
            f"[{sc}]{tr.side.value}[/]",
            f"[grey50]{tr.entry_price:.3f}[/]",
            f"[bright_cyan]+{tr.signal:.1%}[/]",
            f"[{rc}]{res}[/]",
            f"[{pc}]{tr.pnl_pct:+.1f}%[/]",
            sym,
            q,
        )
    console.print(t)

    # Sport breakdown
    sport_stats = result.by_sport()
    if len(sport_stats) > 1:
        console.print()
        st = Table(
            box=box.SIMPLE, show_header=True,
            header_style="bold grey50", padding=(0, 2),
            title="By Sport",
        )
        st.add_column("SPORT",  style="bright_cyan")
        st.add_column("TRADES", justify="right")
        st.add_column("WIN%",   justify="right")
        st.add_column("AVG P&L%", justify="right")
        st.add_column("TOTAL P&L%", justify="right")

        for sport, s in sorted(sport_stats.items(), key=lambda x: -x[1]["pnl"]):
            wr  = s["w"] / s["n"]
            wc2 = "bright_green" if wr >= 0.55 else "bright_red"
            avg = s["pnl"] / s["n"]
            pc2 = "bright_green" if s["pnl"] >= 0 else "bright_red"
            st.add_row(
                sport,
                str(s["n"]),
                f"[{wc2}]{wr:.0%}[/]",
                f"[{pc2}]{avg:+.1f}%[/]",
                f"[{pc2}]{s['pnl']:+.1f}%[/]",
            )
        console.print(st)

    # Calibration note
    console.print()
    console.print(
        "[grey50]Signal = |global_clob - 0.5|  "
        "This measures how well global Polymarket predicted outcomes at entry.[/]"
    )
    if result.win_rate >= 0.55:
        console.print(
            "[bright_green]✓ Global CLOB is predictive — cross-platform arb signal is valid.[/]"
        )
    else:
        console.print(
            "[bright_red]✗ Global CLOB not reliably predictive at this threshold.[/]"
        )
    console.rule()


# ─── CLI ─────────────────────────────────────────────────────────────────────

async def main(
    days_back:    int   = 30,
    min_edge:     float = 0.05,
    hours_before: int   = 6,
) -> None:
    from loguru import logger as _log
    _log.remove()
    _log.add(
        lambda m: console.print(f"[grey50]{m.strip()}[/]"),
        level    = "INFO",
        format   = "{time:HH:mm:ss} | {message}",
        colorize = False,
    )
    console.print(
        f"\n[bold cyan]🏆 Sports Strategy Backtest — "
        f"last {days_back} days  min_edge={min_edge:.0%}  "
        f"entry={hours_before}h before resolution[/]\n"
    )
    result = await run_sports_backtest(
        days_back=days_back,
        min_edge=min_edge,
        hours_before=hours_before,
    )
    print_sports_report(result)


def run() -> None:
    """Sync entry point for `backtest-sports` console script."""
    import sys
    days  = int(sys.argv[1])   if len(sys.argv) > 1 else 30
    edge  = float(sys.argv[2]) if len(sys.argv) > 2 else 0.05
    hours = int(sys.argv[3])   if len(sys.argv) > 3 else 6
    asyncio.run(main(days_back=days, min_edge=edge, hours_before=hours))


if __name__ == "__main__":
    run()
