"""
Terminal dashboard — modular, content-adaptive layout.

Each panel receives its allocated (width, height) at render time and
scales its content — row count, column widths, sparkline length, bar
chart width — to fill the available space proportionally.  When the
terminal is resized, every panel re-flows on the next 0.5 s tick.

Layout:
  ┌──────────────────────────────────────────────────────────────────┐
  │  ● RUNNING    scan #7   next in 73s   last 2.4s   HH:MM:SS UTC  │  ← header
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
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    pass

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

# Fixed column widths used when computing flexible column space
_COL_ID      = 10
_COL_SIDE    = 5
_COL_PRICE   = 7
_COL_UNREAL  = 10
_COL_HRS     = 6
_COL_STRAT   = 5
_COL_PCT     = 6
_COL_EDGE    = 7
_COL_LIQ     = 8
_COL_PNL     = 10
_PANEL_CHROME = 4   # 2 border chars + 2 padding chars per side, each side


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

    # Live crypto market prices (+ spot / sigma from CoinGecko)
    crypto_feed: list = field(default_factory=list)   # list[dict]

    # ── Sports scanner state ──────────────────────────────────────────────────
    sports_scan_number:    int           = 0
    sports_last_scan_at:   datetime | None = None
    sports_scan_duration:  float          = 0.0
    sports_next_scan_in:   float          = 0.0
    sports_matched:        int            = 0    # matched global ↔ US pairs
    sports_opportunities:  list           = field(default_factory=list)
    # Each entry: {slug, title, global_price, us_price, edge, confidence, side}
    sports_feed:           list           = field(default_factory=list)

    # History rings for sparklines (last 30 data points)
    scan_duration_history: deque = field(default_factory=lambda: deque(maxlen=30))
    nav_history:           deque = field(default_factory=lambda: deque(maxlen=30))

    # Daily stats (reset at midnight UTC)
    daily_trades_opened: int   = 0
    daily_trades_closed: int   = 0
    daily_pnl:           float = 0.0
    best_edge_today:     float = 0.0
    _stats_date:         date  = field(default_factory=date.today)

    # Live trading
    live_mode:    bool  = False
    live_balance: float = 0.0

    # Event log
    event_log: deque = field(default_factory=lambda: deque(maxlen=200))

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


# ─── Layout dimension helpers ─────────────────────────────────────────────────

def _panel_dims(term_w: int, term_h: int) -> dict[str, tuple[int, int]]:
    """
    Compute usable (content_width, content_height) for each named panel,
    given the current terminal dimensions.  Values account for panel
    borders (1 char each side) and padding=(0,1) (1 char each side).
    """
    HEADER_H  = 3
    LOG_H     = 8
    BORDER    = 2   # top + bottom border rows
    PAD_SIDES = 2   # left + right padding chars (padding=(0,1))
    BORDER_LR = 2   # left + right border chars

    main_h = max(10, term_h - HEADER_H - LOG_H)

    # Horizontal: ratios 19 : 40 : 28  (total 87)
    left_w   = max(18, int(term_w * 19 / 87))
    right_w  = max(22, int(term_w * 28 / 87))
    center_w = max(28, term_w - left_w - right_w)

    def cw(region_w: int) -> int:
        return max(10, region_w - BORDER_LR - PAD_SIDES)

    def ch(region_h: int) -> int:
        return max(2, region_h - BORDER)

    # Vertical splits
    pos_h    = max(6,  int(main_h * 3 / 5))
    opp_h    = max(6,  main_h - pos_h)

    pnl_h    = max(5,  int(main_h * 3 / 11))
    closed_h = max(4,  int(main_h * 2 / 11))
    sptfeed_h = max(5, int(main_h * 3 / 11))
    wxfeed_h = max(4,  main_h - pnl_h - sptfeed_h - closed_h)

    return {
        "left":          (cw(left_w),    ch(main_h)),
        "positions":     (cw(center_w),  ch(pos_h)  - 2),   # -2: table header+sep
        "opportunities": (cw(center_w),  ch(opp_h)  - 2),
        "pnl":           (cw(right_w),   ch(pnl_h)),
        "sptfeed":       (cw(right_w),   ch(sptfeed_h) - 2),
        "wxfeed":        (cw(right_w),   ch(wxfeed_h) - 2),
        "closed":        (cw(right_w),   ch(closed_h) - 2),
        "log":           (cw(term_w),    ch(LOG_H)),
    }


# ─── Sparkline helpers ────────────────────────────────────────────────────────

_SPARK_CHARS = " ▁▂▃▄▅▆▇█"

def _sparkline(values: list[float], width: int) -> str:
    """Return an ASCII bar-chart string scaled to `width` chars."""
    width = max(4, width)
    if not values:
        return "─" * width
    lo, hi = min(values), max(values)
    span   = (hi - lo) or 1.0
    bars   = []
    for v in values[-width:]:
        idx = int((v - lo) / span * (len(_SPARK_CHARS) - 1))
        bars.append(_SPARK_CHARS[idx])
    bars = [" "] * (width - len(bars)) + bars
    return "".join(bars)


def _nav_spark_colored(values: list[float], width: int) -> Text:
    """Sparkline coloured green if trend is up, red if down."""
    raw = _sparkline(values, width)
    color = C_DIM
    if len(values) >= 2:
        color = C_GREEN if values[-1] >= values[0] else C_RED
    t = Text()
    t.append(raw, style=color)
    return t


# ─── Panel: HEADER ────────────────────────────────────────────────────────────

def _header(state: DashboardState) -> Panel:
    now      = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pulse    = ["◉", "○"][state.tick % 2]

    if state.is_paused:
        status = f"[{C_YELLOW}]⏸  PAUSED[/]"
    else:
        status = f"[{C_GREEN}]{pulse} RUNNING[/]"

    next_s = int(state.next_scan_in)
    center = (
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

def _scanner_panel(state: DashboardState, w: int, h: int) -> Panel:
    state._reset_daily_if_needed()
    total   = max(state.total_markets, 1)
    bar_w   = max(4, min(20, w - 10))
    spark_w = max(4, min(24, w - 10))

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

    # Category bars — always shown
    grid.add_row("", "")
    grid.add_row(f"[{C_DIM}]MARKETS[/]", f"[{C_WHITE}]{state.total_markets}[/]")
    cats = [
        ("WX",  state.weather_mkts,  C_CYAN),
        ("₿",   state.crypto_mkts,   C_YELLOW),
        ("POL", state.politics_mkts, C_MAGENTA),
        ("SPT", state.sports_mkts,   C_GREEN),
        ("OTH", state.other_mkts,    C_DIM),
    ]
    for lbl, cnt, col in cats:
        filled = int((cnt / total) * bar_w)
        bar    = "█" * filled + "░" * (bar_w - filled)
        grid.add_row(
            f"  [{col}]{lbl}[/]",
            f"[{col}]{bar}[/] [{C_DIM}]{cnt}[/]",
        )

    # Scan-time sparkline — shown only when panel is tall enough
    rows_used = 7 + len(cats) + 2  # header rows + cats + blank rows
    if h > rows_used + 3:
        grid.add_row("", "")
        grid.add_row(f"[{C_DIM}]SCAN TIME[/]", "")
        if state.scan_duration_history:
            spark = _sparkline(list(state.scan_duration_history), spark_w)
            avg   = sum(state.scan_duration_history) / len(state.scan_duration_history)
            grid.add_row(f"  [{C_TEAL}]{spark}[/]", f"[{C_DIM}]avg {avg:.1f}s[/]")
        else:
            grid.add_row(f"  [{C_DIM}]{'─' * spark_w}[/]", "")
        rows_used += 3

    # Daily stats — shown only when panel has room for them
    if h > rows_used + 5:
        grid.add_row("", "")
        grid.add_row(f"[{C_DIM}]TODAY[/]", "")
        pnl_col = C_GREEN if state.daily_pnl >= 0 else C_RED
        pnl_sym = "▲" if state.daily_pnl >= 0 else "▼"
        grid.add_row("  P&L",    f"[{pnl_col}]{pnl_sym} ${state.daily_pnl:+.2f}[/]")
        grid.add_row("  OPENED", f"[{C_CYAN}]{state.daily_trades_opened}[/]")
        grid.add_row("  CLOSED", f"[{C_BLUE}]{state.daily_trades_closed}[/]")
        if state.best_edge_today > 0:
            grid.add_row("  BEST",   f"[{C_GREEN}]+{state.best_edge_today:.1%}[/]")
        rows_used += 5

    # Sports scanner stats — shown when sports is enabled and panel has room
    from polybot.config import settings as cfg
    if cfg.sports_enabled and h > rows_used + 4:
        grid.add_row("", "")
        grid.add_row(f"[{C_TEAL}]SPORTS[/]", "")
        if state.sports_scan_number:
            grid.add_row(f"  [{C_DIM}]SCAN #[/]", f"[{C_CYAN}]{state.sports_scan_number}[/]")
        grid.add_row(f"  [{C_DIM}]MATCHED[/]", f"[{C_WHITE}]{state.sports_matched}[/]")
        spt_opps = len(state.sports_opportunities)
        opp_col = C_GREEN if spt_opps > 0 else C_DIM
        grid.add_row(f"  [{C_DIM}]OPPS[/]", f"[{opp_col}]{spt_opps}[/]")
        if state.sports_scan_duration:
            grid.add_row(f"  [{C_DIM}]LAST[/]", f"[{C_DIM}]{state.sports_scan_duration:.1f}s[/]")

    return Panel(
        grid,
        title        = f"[{C_CYAN}]◈ SCANNER[/]",
        border_style = "grey30",
        style        = "on grey7",
        padding      = (0, 1),
    )


# ─── Panel: OPEN POSITIONS ────────────────────────────────────────────────────

def _positions_panel(state: DashboardState, w: int, h: int) -> Panel:
    trader    = state.trader
    max_rows  = max(1, h)

    if trader is None or not trader.positions:
        empty = Align(f"[{C_DIM}]no open positions[/]", align="center", vertical="middle")
        return Panel(
            empty,
            title        = f"[{C_GREEN}]◈ OPEN POSITIONS  [{C_DIM}]0[/][/]",
            border_style = "grey30",
            style        = "on grey7",
            padding      = (0, 1),
        )

    live_prices: dict[str, float] = {m["id"]: m["yes_price"] for m in state.market_feed}
    live_hours:  dict[str, float] = {m["id"]: m["hours_until_close"] for m in state.market_feed}

    # Fixed column widths (always shown)
    fixed_w = _COL_SIDE + _COL_PRICE + _COL_PRICE + _COL_UNREAL + _COL_HRS
    show_id  = w >= 52
    if show_id:
        fixed_w += _COL_ID
    q_len = max(10, w - fixed_w - (10 if show_id else 4))

    t = Table(
        box          = box.SIMPLE,
        show_header  = True,
        header_style = f"bold {C_GREEN}",
        style        = "on grey7",
        expand       = True,
        padding      = (0, 1),
    )
    if show_id:
        t.add_column("ID",     style=C_DIM, no_wrap=True, width=_COL_ID)
    t.add_column("MARKET",     style=C_WHITE, no_wrap=True, max_width=q_len)
    t.add_column("SIDE",       justify="center", width=_COL_SIDE)
    t.add_column("ENTRY",      justify="right",  width=_COL_PRICE)
    t.add_column("NOW",        justify="right",  width=_COL_PRICE)
    t.add_column("UNREAL",     justify="right",  width=_COL_UNREAL)
    t.add_column("HRS",        justify="right",  width=_COL_HRS)

    for trade in list(trader.positions.values())[:max_rows]:
        side_col = C_GREEN if trade.side == "YES" else C_RED

        curr_yes = live_prices.get(trade.market_id)
        if curr_yes is not None:
            curr_side_price = curr_yes if trade.side == "YES" else (1 - curr_yes)
            unreal     = (curr_side_price - trade.entry_price) * trade.shares
            now_str    = f"[{C_WHITE}]{curr_side_price:.3f}[/]"
            unreal_col = C_GREEN if unreal >= 0 else C_RED
            unreal_str = f"[{unreal_col}]{'▲' if unreal >= 0 else '▼'} ${unreal:+.2f}[/]"
        else:
            now_str    = f"[{C_DIM}]—[/]"
            unreal_str = f"[{C_DIM}]—[/]"

        hrs = live_hours.get(trade.market_id)
        if hrs is not None:
            hcol    = C_RED if hrs < 4 else C_YELLOW if hrs < 12 else C_DIM
            hrs_str = f"[{hcol}]{hrs:.1f}[/]"
        else:
            hrs_str = f"[{C_DIM}]—[/]"

        row = []
        if show_id:
            row.append(trade.id[:_COL_ID])
        row += [
            trade.question[:q_len],
            f"[{side_col}]{trade.side}[/]",
            f"[{C_YELLOW}]{trade.entry_price:.3f}[/]",
            now_str,
            unreal_str,
            hrs_str,
        ]
        t.add_row(*row)

    count = len(trader.positions)
    return Panel(
        t,
        title        = f"[{C_GREEN}]◈ OPEN POSITIONS  [{C_WHITE}]{count}[/][/]",
        border_style = "grey30",
        style        = "on grey7",
        padding      = (0, 0),
    )


# ─── Panel: OPPORTUNITIES ─────────────────────────────────────────────────────

def _opportunities_panel(state: DashboardState, w: int, h: int) -> Panel:
    opps     = state.opportunities
    max_rows = max(1, h)

    if not opps:
        empty = Align(f"[{C_DIM}]scanning...[/]", align="center", vertical="middle")
        return Panel(
            empty,
            title        = f"[{C_YELLOW}]◈ OPPORTUNITIES  [{C_DIM}]0[/][/]",
            border_style = "grey30",
            style        = "on grey7",
            padding      = (0, 1),
        )

    show_mdl  = w >= 70
    fixed_w   = _COL_STRAT + _COL_SIDE + _COL_PCT + _COL_EDGE + (4 if show_mdl else 0)
    q_len     = max(10, w - fixed_w - 8)

    t = Table(
        box          = box.SIMPLE,
        show_header  = True,
        header_style = f"bold {C_YELLOW}",
        style        = "on grey7",
        expand       = True,
        padding      = (0, 1),
    )
    t.add_column("ST",     style=C_DIM,   width=_COL_STRAT, no_wrap=True)
    t.add_column("MARKET", style=C_WHITE, no_wrap=True, max_width=q_len)
    t.add_column("SIDE",   justify="center", width=_COL_SIDE)
    t.add_column("MKT%",   justify="right",  width=_COL_PCT)
    if show_mdl:
        t.add_column("MDL%", justify="right", width=_COL_PCT)
    t.add_column("EDGE",   justify="right",  width=_COL_EDGE)

    strat_map = {"weather_trader": "WX", "fast_loop": "BTC", "ai_divergence": "AI"}

    for opp in opps[:max_rows]:
        sc  = C_GREEN if opp.side == "YES" else C_RED
        ec  = C_GREEN if opp.edge >= 0.20 else C_YELLOW if opp.edge >= 0.12 else C_DIM
        lbl = strat_map.get(str(opp.strategy), str(opp.strategy)[:2].upper() or "?")

        row = [
            f"[{C_BLUE}]{lbl}[/]",
            opp.market.question[:q_len],
            f"[{sc}]{opp.side}[/]",
            f"[{C_DIM}]{opp.market_price:.1%}[/]",
        ]
        if show_mdl:
            row.append(f"[{C_WHITE}]{opp.model_probability:.1%}[/]")
        row.append(f"[{ec}]+{opp.edge:.1%}[/]")
        t.add_row(*row)

    return Panel(
        t,
        title        = f"[{C_YELLOW}]◈ OPPORTUNITIES  [{C_WHITE}]{len(opps)}[/][/]",
        border_style = "grey30",
        style        = "on grey7",
        padding      = (0, 0),
    )


# ─── Panel: P&L METRICS ───────────────────────────────────────────────────────

def _pnl_panel(state: DashboardState, w: int, h: int) -> Panel:
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

    closed = trader.closed_trades
    wins   = sum(1 for tr in closed if tr.pnl_usd > 0)
    losses = len(closed) - wins
    wr     = trader.win_rate()
    wrc    = C_GREEN if wr >= 0.55 else C_YELLOW if wr >= 0.45 else C_RED

    spark_w = max(6, min(30, w - 14))

    g = Table.grid(padding=(0, 1))
    g.add_column(style=C_DIM, no_wrap=True)
    g.add_column(justify="right", no_wrap=True)

    if state.live_mode:
        g.add_row(f"[{C_ORANGE}]LIVE USDC[/]", f"[{C_ORANGE}]${state.live_balance:,.2f}[/]")
        g.add_row(f"[{C_DIM}]──── PAPER ────[/]", "")
        g.add_row("BALANCE",   f"[{C_WHITE}]${trader.balance:,.2f}[/]")
    else:
        g.add_row("BALANCE",   f"[{C_WHITE}]${trader.balance:,.2f}[/]")
    g.add_row("OPEN VAL",  f"[{C_BLUE}]${open_val:,.2f}[/]")
    g.add_row("NAV",       f"[{C_CYAN}]${nav:,.2f}[/]")
    g.add_row("", "")
    g.add_row("TOTAL P&L", f"[{pc}]{pa} ${pnl:+,.2f} ({pnl_pct:+.2f}%)[/]")

    if state.nav_history:
        spark = _nav_spark_colored(list(state.nav_history), spark_w)
        g.add_row(f"[{C_DIM}]NAV[/]", spark)

    g.add_row("", "")
    g.add_row("CLOSED",   f"[{C_WHITE}]{len(closed)}[/]  [{C_GREEN}]✓{wins}[/]  [{C_RED}]✗{losses}[/]")
    g.add_row("WIN RATE", f"[{wrc}]{wr:.0%}[/]")
    g.add_row("OPEN",     f"[{C_CYAN}]{len(trader.positions)}[/][{C_DIM}]/5[/]")

    # Show avg win/loss only when there's room
    rows_used = 11
    if h > rows_used + 1 and wins:
        avg_w = sum(tr.pnl_usd for tr in closed if tr.pnl_usd > 0) / wins
        g.add_row("AVG WIN", f"[{C_GREEN}]${avg_w:+.2f}[/]")
        rows_used += 1
    if h > rows_used + 1 and losses:
        avg_l = sum(tr.pnl_usd for tr in closed if tr.pnl_usd < 0) / losses
        g.add_row("AVG LOSS", f"[{C_RED}]${avg_l:+.2f}[/]")

    return Panel(g,
                 title=f"[{C_MAGENTA}]◈ P&L METRICS[/]",
                 border_style="grey30", style="on grey7", padding=(0, 1))


# ─── Panel: MARKET FEED ───────────────────────────────────────────────────────

def _market_feed_panel(state: DashboardState, w: int, h: int) -> Panel:
    max_rows = max(1, h)

    if not state.market_feed:
        empty = Align(f"[{C_DIM}]fetching...[/]", align="center", vertical="middle")
        return Panel(empty,
                     title=f"[{C_ORANGE}]◈ WX FEED[/]",
                     border_style="grey30", style="on grey7")

    show_liq = w >= 55
    show_no  = w >= 45
    fixed_w  = _COL_PCT + _COL_HRS + (6 if show_no else 0) + (_COL_LIQ if show_liq else 0)
    q_len    = max(10, w - fixed_w - 6)

    t = Table(box=box.SIMPLE, show_header=True,
              header_style=f"bold {C_ORANGE}",
              style="on grey7", expand=True, padding=(0, 1))
    t.add_column("MARKET", style=C_WHITE, no_wrap=True, max_width=q_len)
    t.add_column("YES",  justify="right", width=_COL_PCT)
    if show_no:
        t.add_column("NO", justify="right", width=_COL_PCT)
    if show_liq:
        t.add_column("LIQ", justify="right", width=_COL_LIQ, style=C_DIM)
    t.add_column("HRS", justify="right", width=_COL_HRS)

    for m in state.market_feed[:max_rows]:
        yes_p = m.get("yes_price", 0.5)
        no_p  = round(1 - yes_p, 3)
        liq   = m.get("liquidity_usd", 0)
        hrs   = m.get("hours_until_close", 0)
        hc    = C_RED if hrs < 4 else C_YELLOW if hrs < 12 else C_DIM
        yc    = C_GREEN if yes_p >= 0.5 else C_RED

        row = [
            m.get("question", "")[:q_len],
            f"[{yc}]{yes_p:.3f}[/]",
        ]
        if show_no:
            row.append(f"[{C_DIM}]{no_p:.3f}[/]")
        if show_liq:
            row.append(f"${liq:,.0f}")
        row.append(f"[{hc}]{hrs:.0f}h[/]")
        t.add_row(*row)

    return Panel(t,
                 title=f"[{C_ORANGE}]◈ WX FEED  [{C_DIM}]{len(state.market_feed)}[/][/]",
                 border_style="grey30", style="on grey7", padding=(0, 0))


# ─── Panel: SPORTS FEED ──────────────────────────────────────────────────────

def _sports_feed_panel(state: DashboardState, w: int, h: int) -> Panel:
    """
    Shows matched global ↔ US market pairs with the cross-platform edge.

    Columns: GAME · GLOBAL · US · EDGE · CONF
    When sports is disabled or no data yet: shows a one-line status.
    """
    from polybot.config import settings as cfg

    max_rows = max(1, h)

    if not cfg.sports_enabled:
        msg = Align(
            f"[{C_DIM}]sports disabled — set SPORTS_ENABLED=true[/]",
            align="center", vertical="middle",
        )
        return Panel(msg,
                     title=f"[{C_TEAL}]◈ SPT FEED[/]",
                     border_style="grey30", style="on grey7")

    # Header stats row (scan # and last scan time)
    stats_parts: list[str] = []
    if state.sports_scan_number:
        stats_parts.append(f"[{C_DIM}]scan[/] [{C_CYAN}]#{state.sports_scan_number}[/]")
    if state.sports_matched:
        stats_parts.append(f"[{C_DIM}]matched[/] [{C_WHITE}]{state.sports_matched}[/]")
    if state.sports_opportunities:
        stats_parts.append(f"[{C_YELLOW}]{len(state.sports_opportunities)} opps[/]")
    if state.sports_scan_duration:
        stats_parts.append(f"[{C_DIM}]{state.sports_scan_duration:.1f}s[/]")
    next_s = int(state.sports_next_scan_in)
    if next_s > 0:
        stats_parts.append(f"[{C_DIM}]next {next_s}s[/]")

    if not state.sports_feed:
        content = f"  {'  ·  '.join(stats_parts) if stats_parts else ''}\n" if stats_parts else ""
        msg = Align(
            f"{content}[{C_DIM}]waiting for sports scan...[/]",
            align="center", vertical="middle",
        )
        return Panel(msg,
                     title=f"[{C_TEAL}]◈ SPT FEED  [{C_DIM}]—[/][/]",
                     border_style="grey30", style="on grey7")

    # Column widths
    show_conf = w >= 52
    show_slug = w >= 38
    fixed_w   = 6 + 6 + 7 + (5 if show_conf else 0)   # GLOBAL + US + EDGE + CONF
    title_len = max(10, w - fixed_w - (8 if show_slug else 4))

    t = Table(
        box=box.SIMPLE, show_header=True,
        header_style=f"bold {C_TEAL}",
        style="on grey7", expand=True, padding=(0, 1),
    )
    t.add_column("GAME",   style=C_WHITE,  no_wrap=True, max_width=title_len)
    t.add_column("GLOBAL", justify="right", width=6)
    t.add_column("US",     justify="right", width=6)
    t.add_column("EDGE",   justify="right", width=7)
    if show_conf:
        t.add_column("CONF", justify="right", width=5)

    for pair in state.sports_feed[:max_rows]:
        g_price = pair.get("global_price", 0.0)
        u_price = pair.get("us_price",     0.0)
        edge    = pair.get("edge",          0.0)
        conf    = pair.get("confidence",    0.7)
        title   = pair.get("title", pair.get("slug", ""))

        # Edge colour: green = exploitable, yellow = borderline, dim = below threshold
        if abs(edge) >= 0.07:
            ec = C_GREEN
        elif abs(edge) >= 0.04:
            ec = C_YELLOW
        else:
            ec = C_DIM

        # Confidence indicator
        conf_str = {1.0: f"[{C_GREEN}]●●●[/]", 0.7: f"[{C_YELLOW}]●●○[/]"}.get(
            conf, f"[{C_RED}]●○○[/]"
        )

        edge_sign = "+" if edge >= 0 else ""
        row = [
            title[:title_len],
            f"[{C_DIM}]{g_price:.3f}[/]",
            f"[{C_WHITE}]{u_price:.3f}[/]",
            f"[{ec}]{edge_sign}{edge:.3f}[/]",
        ]
        if show_conf:
            row.append(conf_str)
        t.add_row(*row)

    pair_count = len(state.sports_feed)
    stats_str  = "  ·  ".join(stats_parts)
    return Panel(
        t,
        title=(
            f"[{C_TEAL}]◈ SPT FEED  [{C_WHITE}]{pair_count}[/][/]"
            + (f"  [{C_DIM}]{stats_str}[/]" if stats_parts and w > 50 else "")
        ),
        border_style="grey30", style="on grey7", padding=(0, 0),
    )


# ─── Panel: CLOSED TRADES ────────────────────────────────────────────────────

def _closed_panel(state: DashboardState, w: int, h: int) -> Panel:
    trader   = state.trader
    max_rows = max(1, h)

    if trader is None or not trader.closed_trades:
        empty = Align(f"[{C_DIM}]no closed trades[/]", align="center", vertical="middle")
        return Panel(empty,
                     title=f"[{C_BLUE}]◈ CLOSED[/]",
                     border_style="grey30", style="on grey7")

    show_id = w >= 48

    t = Table(box=box.SIMPLE, show_header=True,
              header_style=f"bold {C_BLUE}",
              style="on grey7", expand=True, padding=(0, 1))
    if show_id:
        t.add_column("ID",   style=C_DIM, width=_COL_ID, no_wrap=True)
    t.add_column("SIDE",     justify="center", width=_COL_SIDE)
    t.add_column("IN",       justify="right",  width=_COL_PRICE)
    t.add_column("OUT",      justify="right",  width=_COL_PRICE)
    t.add_column("P&L",      justify="right",  width=_COL_PNL)

    for tr in reversed(trader.closed_trades[-max_rows:]):
        em  = "✓" if tr.pnl_usd >= 0 else "✗"
        pc  = C_GREEN if tr.pnl_usd >= 0 else C_RED
        sc  = C_GREEN if tr.side == "YES" else C_RED
        out = f"{tr.exit_price:.3f}" if tr.exit_price else "—"

        row = []
        if show_id:
            row.append(tr.id[:_COL_ID])
        row += [
            f"[{sc}]{tr.side}[/]",
            f"[{C_DIM}]{tr.entry_price:.3f}[/]",
            f"[{C_WHITE}]{out}[/]",
            f"[{pc}]{em} ${tr.pnl_usd:+.2f}[/]",
        ]
        t.add_row(*row)

    return Panel(t,
                 title=f"[{C_BLUE}]◈ CLOSED  [{C_DIM}]{len(trader.closed_trades)}[/][/]",
                 border_style="grey30", style="on grey7", padding=(0, 0))


# ─── Panel: EVENT LOG ────────────────────────────────────────────────────────

def _log_panel(state: DashboardState, _w: int, h: int) -> Panel:
    max_lines = max(3, h)
    lines     = list(state.event_log)[-max_lines:]
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
        Layout(name="sptfeed", ratio=3),
        Layout(name="wxfeed",  ratio=3),
        Layout(name="closed",  ratio=2),
    )
    return root


def render(layout: Layout, state: DashboardState) -> None:
    state.tick += 1
    tw, th = console.size
    dims   = _panel_dims(tw, th)

    layout["header"].update(_header(state))
    layout["left"].update(_scanner_panel(state,       *dims["left"]))
    layout["positions"].update(_positions_panel(state,       *dims["positions"]))
    layout["opportunities"].update(_opportunities_panel(state,    *dims["opportunities"]))
    layout["pnl"].update(_pnl_panel(state,            *dims["pnl"]))
    layout["sptfeed"].update(_sports_feed_panel(state, *dims["sptfeed"]))
    layout["wxfeed"].update(_market_feed_panel(state,  *dims["wxfeed"]))
    layout["closed"].update(_closed_panel(state,       *dims["closed"]))
    layout["log"].update(_log_panel(state,             *dims["log"]))


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
