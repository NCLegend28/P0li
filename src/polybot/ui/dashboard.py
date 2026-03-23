"""
Terminal dashboard — Moon Dev style, v2.

New in v2:
  - Unrealised P&L per position (computed from live market_feed prices)
  - ASCII sparklines for scan duration history and NAV history
  - DAILY STATS strip (trades today, P&L today, best edge seen)
  - Market feed panel moved into right column
  - Right column: P&L metrics · market feed · closed trades

Layout:
  ┌──────────────────────────────────────────────────────────────────┐
  │  ● SCANNING    scan #7   next in 73s   last 2.4s   HH:MM:SS UTC  │  ← header
  ├──────────────┬──────────────────────────────┬────────────────────┤
  │  ◈ SCANNER   │  ◈ OPEN POSITIONS  2         │  ◈ P&L METRICS     │
  │  status      │  id  market  side  entry  hrs│  balance / nav     │
  │  categories  │  unrealised pnl shown inline │  sparkline         │
  │  sparklines  ├──────────────────────────────┤  ─────────────────  │
  │  daily stats │  ◈ OPPORTUNITIES  7          │  ◈ WEATHER FEED    │
  │              │  strat market side mkt% edge │  market prices     │
  │              │                              │  ─────────────────  │
  │              │                              │  ◈ CLOSED TRADES   │
  ├──────────────┴──────────────────────────────┴────────────────────┤
  │  ◈ EVENT LOG                                                      │  ← log strip
  └──────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from typing import TYPE_CHECKING

from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from polybot.models import Opportunity, PaperTrade
    from polybot.paper.trader import PaperTrader

console = Console()

# ─── Colour palette ───────────────────────────────────────────────────────────
C_CYAN    = "bright_cyan"
C_GREEN   = "bright_green"
C_RED     = "bright_red"
C_YELLOW  = "bright_yellow"
C_MAGENTA = "magenta"
C_DIM     = "grey50"
C_WHITE   = "white"
C_ORANGE  = "dark_orange"
C_BLUE    = "dodger_blue2"
C_TEAL    = "dark_cyan"


# ─── Shared state ─────────────────────────────────────────────────────────────

@dataclass
class DashboardState:
    # Scanner metadata
    scan_number:      int   = 0
    is_running:       bool  = True
    is_paused:        bool  = False
    last_scan_at:     datetime | None = None
    next_scan_in:     float = 0.0
    scan_duration:    float = 0.0
    scan_interval:    int   = 120

    # Market counts
    total_markets:    int = 0
    weather_mkts:     int = 0
    crypto_mkts:      int = 0
    politics_mkts:    int = 0
    sports_mkts:      int = 0
    other_mkts:       int = 0
    forecasts_fetched:int = 0

    # Opportunities from last scan
    opportunities: list = field(default_factory=list)

    # Trader reference
    trader: object = None

    # Live weather market prices
    market_feed: list = field(default_factory=list)   # list[dict]

    # History rings for sparklines (last 20 data points)
    scan_duration_history: deque = field(default_factory=lambda: deque(maxlen=20))
    nav_history:           deque = field(default_factory=lambda: deque(maxlen=20))

    # Daily stats (reset at midnight UTC)
    daily_trades_opened: int   = 0
    daily_trades_closed: int   = 0
    daily_pnl:           float = 0.0
    best_edge_today:     float = 0.0
    _stats_date:         date  = field(default_factory=date.today)

    # Event log
    event_log: deque = field(default_factory=lambda: deque(maxlen=7))

    # Animation tick
    tick: int = 0

    def record_scan(self, duration: float, nav: float) -> None:
        self.scan_duration_history.append(duration)
        self.nav_history.append(nav)

    def _reset_daily_if_needed(self) -> None:
        today = date.today()
        if today != self._stats_date:
            self.daily_trades_opened = 0
            self.daily_trades_closed = 0
            self.daily_pnl           = 0.0
            self.best_edge_today     = 0.0
            self._stats_date         = today


# ─── Sparkline helpers ────────────────────────────────────────────────────────
# Braille block chars give 8 height levels in a single character column.
# We use a simpler 8-bar unicode set that works in most terminals.

_SPARK_CHARS = " ▁▂▃▄▅▆▇█"

def _sparkline(values: list[float], width: int = 16) -> str:
    """Return a mini ASCII bar chart string of `width` chars."""
    if not values:
        return C_DIM + "─" * width
    lo, hi = min(values), max(values)
    span   = (hi - lo) or 1.0
    bars   = []
    for v in values[-width:]:
        idx = int((v - lo) / span * (len(_SPARK_CHARS) - 1))
        bars.append(_SPARK_CHARS[idx])
    # Pad left if shorter than width
    bars = [" "] * (width - len(bars)) + bars
    return "".join(bars)


def _nav_spark_colored(values: list[float], width: int = 16) -> Text:
    """Sparkline coloured green if trend is up, red if down."""
    raw = _sparkline(values, width)
    if len(values) >= 2:
        color = C_GREEN if values[-1] >= values[0] else C_RED
    else:
        color = C_DIM
    t = Text()
    t.append(raw, style=color)
    return t


# ─── Panel: HEADER ────────────────────────────────────────────────────────────

def _header(state: DashboardState) -> Panel:
    now  = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    pulse  = ["◉", "○"][state.tick % 2]
    if state.is_paused:
        status = f"[{C_YELLOW}]⏸  PAUSED[/]"
    else:
        status = f"[{C_GREEN}]{pulse} RUNNING[/]"

    next_s   = int(state.next_scan_in)
    center   = (
        f"[{C_DIM}]scan[/] [{C_CYAN}]#{state.scan_number}[/]"
        f"  [{C_DIM}]·[/]  "
        f"[{C_DIM}]next in[/] [{C_CYAN}]{next_s}s[/]"
    )
    if state.scan_duration:
        center += f"  [{C_DIM}]·[/]  [{C_DIM}]last[/] [{C_YELLOW}]{state.scan_duration:.1f}s[/]"

    right = f"[{C_DIM}]{date_str}[/]  [{C_WHITE}]⏱ {now}[/]"

    content = Columns([
        Align(Text.from_markup(status), align="left"),
        Align(Text.from_markup(center), align="center"),
        Align(Text.from_markup(right),  align="right"),
    ], expand=True)

    return Panel(
        content,
        style        = "on grey7",
        border_style = C_CYAN,
        padding      = (0, 1),
    )


# ─── Panel: SCANNER LEFT ──────────────────────────────────────────────────────

def _scanner_panel(state: DashboardState) -> Panel:
    state._reset_daily_if_needed()
    total = max(state.total_markets, 1)

    grid = Table.grid(padding=(0, 1))
    grid.add_column(style=C_DIM,   no_wrap=True)
    grid.add_column(style=C_WHITE, no_wrap=True, justify="right")

    sc = C_YELLOW if state.is_paused else C_GREEN
    grid.add_row("STATUS",    f"[{sc}]{'PAUSED' if state.is_paused else 'RUNNING'}[/]")
    grid.add_row("SCAN #",    f"[{C_CYAN}]{state.scan_number}[/]")
    last = state.last_scan_at.strftime("%H:%M:%S") if state.last_scan_at else "—"
    grid.add_row("LAST",      f"[{C_DIM}]{last}[/]")
    grid.add_row("INTERVAL",  f"[{C_DIM}]{state.scan_interval}s[/]")
    grid.add_row("FORECASTS", f"[{C_BLUE}]{state.forecasts_fetched}[/]")

    # ── Category bars ──────────────────────────────────────────────────────────
    grid.add_row("", "")
    grid.add_row(f"[{C_DIM}]MARKETS[/]", f"[{C_WHITE}]{state.total_markets}[/]")

    BAR_W = 12
    cats = [
        ("WX",   state.weather_mkts,  C_CYAN),
        ("₿",    state.crypto_mkts,   C_YELLOW),
        ("POL",  state.politics_mkts, C_MAGENTA),
        ("SPT",  state.sports_mkts,   C_GREEN),
        ("OTH",  state.other_mkts,    C_DIM),
    ]
    for lbl, cnt, col in cats:
        filled = int((cnt / total) * BAR_W)
        bar    = "█" * filled + "░" * (BAR_W - filled)
        grid.add_row(
            f"  [{col}]{lbl}[/]",
            f"[{col}]{bar}[/] [{C_DIM}]{cnt}[/]",
        )

    # ── Scan duration sparkline ────────────────────────────────────────────────
    grid.add_row("", "")
    grid.add_row(f"[{C_DIM}]SCAN TIME[/]", "")
    if state.scan_duration_history:
        spark = _sparkline(list(state.scan_duration_history), 14)
        avg   = sum(state.scan_duration_history) / len(state.scan_duration_history)
        grid.add_row(
            f"  [{C_TEAL}]{spark}[/]",
            f"[{C_DIM}]avg {avg:.1f}s[/]",
        )
    else:
        grid.add_row(f"  [{C_DIM}]─────────────[/]", "")

    # ── Daily stats ────────────────────────────────────────────────────────────
    grid.add_row("", "")
    grid.add_row(f"[{C_DIM}]TODAY[/]", "")
    pnl_col = C_GREEN if state.daily_pnl >= 0 else C_RED
    pnl_sym = "▲" if state.daily_pnl >= 0 else "▼"
    grid.add_row("  P&L",    f"[{pnl_col}]{pnl_sym} ${state.daily_pnl:+.2f}[/]")
    grid.add_row("  OPENED", f"[{C_CYAN}]{state.daily_trades_opened}[/]")
    grid.add_row("  CLOSED", f"[{C_BLUE}]{state.daily_trades_closed}[/]")
    if state.best_edge_today > 0:
        grid.add_row(
            "  BEST EDGE",
            f"[{C_GREEN}]+{state.best_edge_today:.1%}[/]",
        )

    return Panel(
        grid,
        title        = f"[{C_CYAN}]◈ SCANNER[/]",
        border_style = "grey30",
        style        = "on grey7",
        padding      = (0, 1),
    )


# ─── Panel: OPEN POSITIONS ────────────────────────────────────────────────────

def _positions_panel(state: DashboardState) -> Panel:
    trader = state.trader
    if trader is None or not trader.positions:
        empty = Align(f"[{C_DIM}]no open positions[/]", align="center", vertical="middle")
        return Panel(
            empty,
            title        = f"[{C_GREEN}]◈ OPEN POSITIONS  [{C_DIM}]0[/][/]",
            border_style = "grey30",
            style        = "on grey7",
            padding      = (0, 1),
        )

    # Build current-price lookup from market feed
    live_prices: dict[str, float] = {
        m["id"]: m["yes_price"] for m in state.market_feed
    }
    live_hours: dict[str, float] = {
        m["id"]: m["hours_until_close"] for m in state.market_feed
    }

    t = Table(
        box          = box.SIMPLE,
        show_header  = True,
        header_style = f"bold {C_GREEN}",
        style        = "on grey7",
        expand       = True,
        padding      = (0, 1),
    )
    t.add_column("ID",      style=C_DIM,  no_wrap=True, width=12)
    t.add_column("MARKET",  style=C_WHITE, no_wrap=True)
    t.add_column("SIDE",    justify="center", width=5)
    t.add_column("ENTRY",   justify="right",  width=7)
    t.add_column("NOW",     justify="right",  width=7)
    t.add_column("UNREAL",  justify="right",  width=10)
    t.add_column("HRS",     justify="right",  width=6)

    for trade in trader.positions.values():
        side_col = C_GREEN if trade.side == "YES" else C_RED

        # Compute unrealised P&L from live price
        curr_yes = live_prices.get(trade.market_id)
        if curr_yes is not None:
            from polybot.models import Side
            curr_side_price = curr_yes if trade.side == "YES" else (1 - curr_yes)
            unreal = (curr_side_price - trade.entry_price) * trade.shares
            now_str   = f"[{C_WHITE}]{curr_side_price:.3f}[/]"
            unreal_col = C_GREEN if unreal >= 0 else C_RED
            unreal_str = f"[{unreal_col}]{'▲' if unreal >= 0 else '▼'} ${unreal:+.2f}[/]"
        else:
            now_str    = f"[{C_DIM}]—[/]"
            unreal_str = f"[{C_DIM}]—[/]"

        hrs = live_hours.get(trade.market_id)
        if hrs is not None:
            hcol   = C_RED if hrs < 4 else C_YELLOW if hrs < 12 else C_DIM
            hrs_str = f"[{hcol}]{hrs:.1f}[/]"
        else:
            hrs_str = f"[{C_DIM}]—[/]"

        t.add_row(
            trade.id[:8],
            trade.question[:36],
            f"[{side_col}]{trade.side}[/]",
            f"[{C_YELLOW}]{trade.entry_price:.3f}[/]",
            now_str,
            unreal_str,
            hrs_str,
        )

    count = len(trader.positions)
    return Panel(
        t,
        title        = f"[{C_GREEN}]◈ OPEN POSITIONS  [{C_WHITE}]{count}[/][/]",
        border_style = "grey30",
        style        = "on grey7",
        padding      = (0, 0),
    )


# ─── Panel: OPPORTUNITIES ────────────────────────────────────────────────────

def _opportunities_panel(state: DashboardState) -> Panel:
    opps = state.opportunities
    if not opps:
        empty = Align(f"[{C_DIM}]scanning...[/]", align="center", vertical="middle")
        return Panel(
            empty,
            title        = f"[{C_YELLOW}]◈ OPPORTUNITIES  [{C_DIM}]0[/][/]",
            border_style = "grey30",
            style        = "on grey7",
            padding      = (0, 1),
        )

    t = Table(
        box          = box.SIMPLE,
        show_header  = True,
        header_style = f"bold {C_YELLOW}",
        style        = "on grey7",
        expand       = True,
        padding      = (0, 1),
    )
    t.add_column("STRAT",  style=C_DIM,   width=5, no_wrap=True)
    t.add_column("MARKET", style=C_WHITE, no_wrap=True)
    t.add_column("SIDE",   justify="center", width=5)
    t.add_column("MKT%",   justify="right",  width=6)
    t.add_column("MDL%",   justify="right",  width=6)
    t.add_column("EDGE",   justify="right",  width=7)

    for opp in opps[:6]:
        sc  = C_GREEN if opp.side == "YES" else C_RED
        ec  = C_GREEN if opp.edge >= 0.20 else C_YELLOW if opp.edge >= 0.12 else C_DIM
        strat_map = {"weather_trader": "WX", "fast_loop": "BTC", "ai_divergence": "AI"}
        lbl = strat_map.get(str(opp.strategy), str(opp.strategy)[:3].upper() or "?")

        t.add_row(
            f"[{C_BLUE}]{lbl}[/]",
            opp.market.question[:40],
            f"[{sc}]{opp.side}[/]",
            f"[{C_DIM}]{opp.market_price:.1%}[/]",
            f"[{C_WHITE}]{opp.model_probability:.1%}[/]",
            f"[{ec}]+{opp.edge:.1%}[/]",
        )

    return Panel(
        t,
        title        = f"[{C_YELLOW}]◈ OPPORTUNITIES  [{C_WHITE}]{len(opps)}[/][/]",
        border_style = "grey30",
        style        = "on grey7",
        padding      = (0, 0),
    )


# ─── Panel: P&L METRICS ───────────────────────────────────────────────────────

def _pnl_panel(state: DashboardState) -> Panel:
    trader = state.trader
    if trader is None:
        return Panel(f"[{C_DIM}]no trader[/]",
                     title=f"[{C_MAGENTA}]◈ P&L[/]",
                     border_style="grey30", style="on grey7")

    starting = trader._starting_balance()
    open_val = sum(t.size_usd for t in trader.positions.values())
    nav      = trader.balance + open_val
    pnl      = nav - starting
    pnl_pct  = (pnl / starting) * 100
    pc       = C_GREEN if pnl >= 0 else C_RED
    pa       = "▲" if pnl >= 0 else "▼"

    closed   = trader.closed_trades
    wins     = sum(1 for tr in closed if tr.pnl_usd > 0)
    losses   = len(closed) - wins
    wr       = trader.win_rate()
    wrc      = C_GREEN if wr >= 0.55 else C_YELLOW if wr >= 0.45 else C_RED

    g = Table.grid(padding=(0, 1))
    g.add_column(style=C_DIM, no_wrap=True)
    g.add_column(justify="right", no_wrap=True)

    g.add_row("BALANCE",    f"[{C_WHITE}]${trader.balance:,.2f}[/]")
    g.add_row("OPEN VAL",   f"[{C_BLUE}]${open_val:,.2f}[/]")
    g.add_row("NAV",        f"[{C_CYAN}]${nav:,.2f}[/]")
    g.add_row("", "")
    g.add_row("TOTAL P&L",  f"[{pc}]{pa} ${pnl:+,.2f} ({pnl_pct:+.2f}%)[/]")

    # NAV sparkline
    if state.nav_history:
        spark = _nav_spark_colored(list(state.nav_history), 14)
        g.add_row(f"  [{C_DIM}]NAV HIST[/]", spark)
    g.add_row("", "")
    g.add_row("CLOSED",   f"[{C_WHITE}]{len(closed)}[/]  [{C_GREEN}]✓{wins}[/]  [{C_RED}]✗{losses}[/]")
    g.add_row("WIN RATE", f"[{wrc}]{wr:.0%}[/]")
    g.add_row("OPEN",     f"[{C_CYAN}]{len(trader.positions)}[/][{C_DIM}]/5[/]")

    if wins:
        avg_w = sum(tr.pnl_usd for tr in closed if tr.pnl_usd > 0) / wins
        g.add_row("AVG WIN",  f"[{C_GREEN}]${avg_w:+.2f}[/]")
    if losses:
        avg_l = sum(tr.pnl_usd for tr in closed if tr.pnl_usd < 0) / losses
        g.add_row("AVG LOSS", f"[{C_RED}]${avg_l:+.2f}[/]")

    return Panel(g,
                 title=f"[{C_MAGENTA}]◈ P&L METRICS[/]",
                 border_style="grey30", style="on grey7", padding=(0, 1))


# ─── Panel: MARKET FEED ───────────────────────────────────────────────────────

def _market_feed_panel(state: DashboardState) -> Panel:
    if not state.market_feed:
        empty = Align(f"[{C_DIM}]fetching...[/]", align="center", vertical="middle")
        return Panel(empty,
                     title=f"[{C_ORANGE}]◈ WX FEED[/]",
                     border_style="grey30", style="on grey7")

    t = Table(box=box.SIMPLE, show_header=True,
              header_style=f"bold {C_ORANGE}",
              style="on grey7", expand=True, padding=(0, 1))
    t.add_column("MARKET", style=C_WHITE, no_wrap=True, min_width=28)
    t.add_column("YES",  justify="right", width=6)
    t.add_column("NO",   justify="right", width=6)
    t.add_column("LIQ",  justify="right", width=7, style=C_DIM)
    t.add_column("HRS",  justify="right", width=5)

    for m in state.market_feed[:7]:
        yes_p = m.get("yes_price", 0.5)
        no_p  = round(1 - yes_p, 3)
        liq   = m.get("liquidity_usd", 0)
        hrs   = m.get("hours_until_close", 0)
        hc    = C_RED if hrs < 4 else C_YELLOW if hrs < 12 else C_DIM
        # Colour YES green if > 0.5, red if < 0.5
        yc    = C_GREEN if yes_p >= 0.5 else C_RED

        t.add_row(
            m.get("question", "")[:30],
            f"[{yc}]{yes_p:.3f}[/]",
            f"[{C_DIM}]{no_p:.3f}[/]",
            f"${liq:,.0f}",
            f"[{hc}]{hrs:.0f}h[/]",
        )

    return Panel(t,
                 title=f"[{C_ORANGE}]◈ WX FEED  [{C_DIM}]{len(state.market_feed)}[/][/]",
                 border_style="grey30", style="on grey7", padding=(0, 0))


# ─── Panel: CLOSED TRADES ────────────────────────────────────────────────────

def _closed_panel(state: DashboardState) -> Panel:
    trader = state.trader
    if trader is None or not trader.closed_trades:
        empty = Align(f"[{C_DIM}]no closed trades[/]", align="center", vertical="middle")
        return Panel(empty,
                     title=f"[{C_BLUE}]◈ CLOSED[/]",
                     border_style="grey30", style="on grey7")

    t = Table(box=box.SIMPLE, show_header=True,
              header_style=f"bold {C_BLUE}",
              style="on grey7", expand=True, padding=(0, 1))
    t.add_column("ID",    style=C_DIM, width=10, no_wrap=True)
    t.add_column("SIDE",  justify="center", width=5)
    t.add_column("IN",    justify="right",  width=7)
    t.add_column("OUT",   justify="right",  width=7)
    t.add_column("P&L",   justify="right",  width=10)

    for tr in reversed(trader.closed_trades[-6:]):
        em  = "✓" if tr.pnl_usd >= 0 else "✗"
        pc  = C_GREEN if tr.pnl_usd >= 0 else C_RED
        sc  = C_GREEN if tr.side == "YES" else C_RED
        out = f"{tr.exit_price:.3f}" if tr.exit_price else "—"
        t.add_row(
            tr.id[:8],
            f"[{sc}]{tr.side}[/]",
            f"[{C_DIM}]{tr.entry_price:.3f}[/]",
            f"[{C_WHITE}]{out}[/]",
            f"[{pc}]{em} ${tr.pnl_usd:+.2f}[/]",
        )

    return Panel(t,
                 title=f"[{C_BLUE}]◈ CLOSED  [{C_DIM}]{len(trader.closed_trades)}[/][/]",
                 border_style="grey30", style="on grey7", padding=(0, 0))


# ─── Panel: EVENT LOG ────────────────────────────────────────────────────────

def _log_panel(state: DashboardState) -> Panel:
    lines = list(state.event_log)
    if not lines:
        lines = [f"[{C_DIM}]System ready. Waiting for first scan...[/]"]
    text = Text()
    for i, line in enumerate(lines):
        if i:
            text.append("\n")
        text.append_text(Text.from_markup(line))
    return Panel(text,
                 title=f"[{C_DIM}]◈ EVENT LOG[/]",
                 border_style="grey23", style="on grey7", padding=(0, 1))


# ─── Layout ───────────────────────────────────────────────────────────────────

def build_layout() -> Layout:
    root = Layout()
    root.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="log",    size=8),
    )
    root["main"].split_row(
        Layout(name="left",   ratio=19),
        Layout(name="center", ratio=40),
        Layout(name="right",  ratio=28),
    )
    root["center"].split_column(
        Layout(name="positions",     ratio=3),
        Layout(name="opportunities", ratio=2),
    )
    root["right"].split_column(
        Layout(name="pnl",     ratio=3),
        Layout(name="wxfeed",  ratio=4),
        Layout(name="closed",  ratio=3),
    )
    return root


def render(layout: Layout, state: DashboardState) -> None:
    state.tick += 1
    layout["header"].update(_header(state))
    layout["left"].update(_scanner_panel(state))
    layout["positions"].update(_positions_panel(state))
    layout["opportunities"].update(_opportunities_panel(state))
    layout["pnl"].update(_pnl_panel(state))
    layout["wxfeed"].update(_market_feed_panel(state))
    layout["closed"].update(_closed_panel(state))
    layout["log"].update(_log_panel(state))


# ─── Dashboard class ─────────────────────────────────────────────────────────

class Dashboard:
    def __init__(self, state: DashboardState):
        self.state  = state
        self.layout = build_layout()
        self._live  = Live(
            self.layout,
            console            = console,
            refresh_per_second = 2,
            screen             = True,
        )

    def __enter__(self) -> "Dashboard":
        self._live.__enter__()
        return self

    def __exit__(self, *args) -> None:
        self._live.__exit__(*args)

    def log(self, msg: str, level: str = "INFO") -> None:
        color = {"INFO": C_WHITE, "GOOD": C_GREEN, "WARN": C_YELLOW,
                 "ERROR": C_RED, "TRADE": C_CYAN, "EXIT": C_MAGENTA}.get(level.upper(), C_DIM)
        tag   = {"INFO": "·", "GOOD": "✓", "WARN": "⚠", "ERROR": "✗",
                 "TRADE": "◉", "EXIT": "◎"}.get(level.upper(), "·")
        ts    = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.state.event_log.append(
            f"[{C_DIM}]{ts}[/]  [{color}]{tag}[/]  {msg}"
        )

    async def run_renderer(self) -> None:
        while self.state.is_running:
            render(self.layout, self.state)
            await asyncio.sleep(0.5)