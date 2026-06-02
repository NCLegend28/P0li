"""
Trading engine — handles both simulated (paper) and live trading.

The dual-mode engine:
  - Simulated mode: Virtual balance, no real capital at risk
  - Live mode: Real orders via CLOB API, actual PnL

Responsibilities:
  - Maintain positions (simulated or live-synced)
  - Open/close positions based on strategy signals
  - Persist trade log to JSONL (one trade per line, easy to parse later)
  - Enforce position limits and max position size
  - Print a live P&L dashboard to the terminal
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from rich.console import Console
from rich.table import Table
from rich import box

from polybot.config import settings
from polybot.models import Opportunity, TradeRecord, Side, TradeStatus
from polybot.strategies.exit import ExitReason

TRADE_LOG_PATH = Path("data/trades/trades.jsonl")
console = Console()


class TradingEngine:
    def __init__(self):
        self.balance:      float             = settings.simulated_starting_balance
        self.positions:    dict[str, TradeRecord] = {}   # opportunity_id → trade
        self.closed_trades: list[TradeRecord] = []
        self._clob    = None   # global CLOB client — set by cli.py when LIVE_TRADING=true
        self._us_clob = None   # US platform client — set by cli.py when US keys configured
        self._live_starting_balance: float | None = None

        TRADE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._load_history()

    @property
    def live_mode(self) -> bool:
        """True when live trading is enabled and global CLOB client is wired in."""
        return settings.live_trading and self._clob is not None

    @property
    def us_live_mode(self) -> bool:
        """True when live trading is enabled and US platform client is wired in."""
        return settings.live_trading and self._us_clob is not None

    def _venue_is_live(self, opp: Opportunity) -> bool:
        """Route an opportunity to live execution iff its venue has a live client
        wired in. Sports opportunities use the Polymarket US client; everything
        else uses the global CLOB. With LIVE_TRADING=true and
        SPORTS_ALERT_ONLY=true the user gets live weather + paper sports."""
        if opp.us_market_slug:
            return self.us_live_mode
        return self.live_mode

    def set_clob_client(self, clob) -> None:
        """Wire in the global CLOB client. Called by cli.py at startup."""
        self._clob = clob
        self._live_starting_balance = clob.get_balance()
        logger.info(f"Live trading enabled (global) — balance=${self._live_starting_balance:.2f}")

    def set_us_client(self, us_client) -> None:
        """Wire in the Polymarket US client. Called by cli.py at startup."""
        self._us_clob = us_client
        bal = us_client.get_balance()
        logger.info("Live trading enabled (US platform) — balance={}", bal)

    # ─── State persistence ────────────────────────────────────────────────────

    def _load_history(self) -> None:
        if not TRADE_LOG_PATH.exists():
            logger.info("No existing trade log found — starting fresh")
            return

        # Reconcile per opportunity_id — later records supersede earlier ones.
        # An opp_id closed in the log is closed; only opps whose latest record is
        # still status=open should be re-loaded as live positions. Without this,
        # a restart re-injects already-closed trades into self.positions and the
        # next scan closes them again, corrupting the trade log.
        latest: dict[str, TradeRecord] = {}
        with TRADE_LOG_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                trade = TradeRecord.model_validate_json(line)
                latest[trade.opportunity_id] = trade

        for trade in latest.values():
            if trade.status == TradeStatus.OPEN:
                self.positions[trade.opportunity_id] = trade
            else:
                self.closed_trades.append(trade)

        # Recompute balance from closed trades
        starting = self._starting_balance()
        total_pnl = sum(t.pnl_usd for t in self.closed_trades)
        open_capital = sum(t.size_usd for t in self.positions.values())
        self.balance = starting + total_pnl - open_capital

        logger.info(
            f"Loaded {len(self.closed_trades)} closed + "
            f"{len(self.positions)} open positions | "
            f"balance=${self.balance:.2f}"
        )

    def _append_trade(self, trade: TradeRecord) -> None:
        with TRADE_LOG_PATH.open("a") as f:
            f.write(trade.model_dump_json() + "\n")

    # ─── Position management ──────────────────────────────────────────────────

    def _size_position(self, opp: Opportunity) -> float:
        """Confidence-weighted fractional Kelly, capped by strategy hint, global cap, and exposure budget.

        Full Kelly fraction for a binary $1-payoff contract: f* = edge / (1 - entry_price).
        We use 0.25 × confidence as the Kelly multiplier — quarter-Kelly is the standard
        safe target, scaled down further when the model is less confident in its edge.

        Sports v1 override: if `sports_flat_size_usd` > 0 and this is a sports
        opportunity (`opp.us_market_slug` is set), return that flat amount —
        still clamped by balance and the 40% portfolio exposure cap.
        """
        price = opp.market_price
        if not (0.0 < price < 1.0) or opp.edge <= 0:
            return 0.0

        # NAV = cash + capital tied up in open positions. We cap deployed
        # capital at 40% of NAV, NOT 40% of remaining cash — otherwise the
        # cap shrinks as you deploy and collapses to $0 in a feedback loop.
        open_exposure    = sum(t.size_usd for t in self.positions.values())
        nav              = self.balance + open_exposure
        remaining_budget = max(0.0, nav * 0.40 - open_exposure)

        is_sports_opp = bool(opp.us_market_slug)
        if is_sports_opp and settings.sports_flat_size_usd > 0:
            return max(0.0, min(settings.sports_flat_size_usd, self.balance, remaining_budget))

        full_kelly = opp.edge / (1.0 - price)
        kelly_fraction = 0.25 * max(0.0, min(opp.confidence, 1.0))
        raw_size = self.balance * full_kelly * kelly_fraction

        # Per-venue cap: a sports paper trade riding alongside a live weather
        # book should use the paper cap, not the live cap, even though
        # LIVE_TRADING is on globally.
        opp_is_live = self._venue_is_live(opp)
        global_cap = settings.live_max_position_usd if opp_is_live else settings.simulated_max_position_usd

        # Cheap-tail boost: at entry <= cheap_tail_threshold the audit shows
        # the bucket is profitable; multiply the cap so Kelly can size into it.
        if price <= settings.cheap_tail_threshold and settings.cheap_tail_size_multiplier > 1.0:
            global_cap = global_cap * settings.cheap_tail_size_multiplier

        strategy_cap = opp.size_usd if opp.size_usd > 0 else global_cap

        # remaining_budget computed once at the top against NAV (not cash).
        return max(0.0, min(raw_size, global_cap, strategy_cap, remaining_budget))

    def open_position(self, opp: Opportunity) -> TradeRecord | None:
        if len(self.positions) >= settings.max_open_positions:
            logger.debug(f"Max positions reached ({settings.max_open_positions}), skipping")
            return None

        if opp.id in self.positions:
            logger.debug(f"Already have position for opportunity {opp.id}")
            return None

        size_usd = self._size_position(opp)
        if size_usd < 1.0:
            logger.debug(
                "Skipping {} — sized to ${:.2f} (edge={:.3f} conf={:.2f} bal=${:.2f})",
                opp.market.question[:40], size_usd, opp.edge, opp.confidence, self.balance,
            )
            return None

        shares       = size_usd / opp.market_price
        is_sports    = bool(opp.us_market_slug)
        execute_live = self._venue_is_live(opp)

        # `live_platform` is a venue marker (NOT a "real order placed" marker).
        # Sports paper trades carry "polymarket_us" so the sports exit pipeline
        # in scanner/sports_graph.py finds them via the same filter as live ones.
        venue = "polymarket_us" if is_sports else "polymarket_global"

        trade = TradeRecord(
            opportunity_id = opp.id,
            market_id      = opp.market.id,
            question       = opp.market.question,
            side           = opp.side,
            entry_price    = opp.market_price,
            size_usd       = size_usd,
            shares         = shares,
            live_platform  = venue,
            us_market_slug = opp.us_market_slug or None,
        )

        if execute_live and is_sports:
            quantity = max(1, int(size_usd / opp.market_price))
            order = self._us_clob.place_order(
                market_slug = opp.us_market_slug,
                side        = str(opp.side),
                price       = opp.market_price,
                quantity    = quantity,
            )
            if not order:
                logger.warning("US live order FAILED for {} — skipping", opp.market.question[:45])
                return None
            trade = trade.model_copy(update={"live_order_id": order.get("id")})
            logger.success(
                "LIVE ORDER PLACED (US) | {} {} @ {:.3f} | ${:.2f} | order_id={}",
                trade.side, trade.question[:45], trade.entry_price, size_usd, order.get("id"),
            )

        elif execute_live and not is_sports:
            token_id = opp.clob_token_id
            if not token_id:
                logger.warning("No CLOB token ID for {} — skipping", opp.market.question[:45])
                return None
            order_id = self._clob.place_order(
                token_id = token_id,
                side     = str(opp.side),
                price    = opp.market_price,
                size_usd = size_usd,
            )
            if not order_id:
                logger.warning("Global CLOB order FAILED for {} — skipping", opp.market.question[:45])
                return None
            trade = trade.model_copy(update={"clob_order_id": order_id, "clob_token_id": token_id})
            logger.success(
                "LIVE ORDER PLACED (global) | {} {} @ {:.3f} | ${:.2f} | order_id={}",
                trade.side, trade.question[:45], trade.entry_price, size_usd, order_id,
            )

        else:
            # Paper trade — decrement the paper book. No real order placed.
            self.balance -= size_usd
            logger.info(
                f"PAPER OPEN  {trade.side} {trade.question[:45]}... "
                f"@ {trade.entry_price:.3f} | ${size_usd:.2f} | "
                f"venue={venue} opp_id={opp.id}"
            )

        self.positions[opp.id] = trade
        self._append_trade(trade)
        return trade

    def close_position(
        self,
        opportunity_id: str,
        exit_price: float,
        exit_reason: ExitReason | None = None,
    ) -> TradeRecord:
        trade = self.positions.pop(opportunity_id)

        trade = trade.model_copy(update={
            "status":     TradeStatus.CLOSED,
            "exit_price": exit_price,
            "closed_at":  datetime.now(timezone.utc),
        })

        # A trade is "paper" iff no real order id was ever recorded for it.
        # Per-trade routing matters when LIVE_TRADING=true but a venue's
        # client is unwired (e.g. SPORTS_ALERT_ONLY=true): weather trades on
        # the same engine are live, sports trades are paper.
        was_live = bool(trade.clob_order_id or trade.live_order_id)
        if not was_live:
            proceeds = exit_price * trade.shares
            self.balance += proceeds

        self.closed_trades.append(trade)
        self._append_trade(trade)

        emoji = "✅" if trade.pnl_usd >= 0 else "❌"
        logger.info(
            f"{emoji} CLOSE {trade.side} {trade.question[:45]}... "
            f"@ {exit_price:.3f} | PnL=${trade.pnl_usd:+.2f} ({trade.pnl_pct:+.1f}%)"
        )

        # ── Live execution — place sell / close orders ────────────────────────
        # MARKET_CLOSED uses synthetic 1.0/0.0 exit prices that no real order would
        # fill at. Skip the live sell and let Polymarket auto-settle the position
        # on resolution — paper book has already recorded P&L for tracking.
        hold_to_settlement = exit_reason == ExitReason.MARKET_CLOSED

        if trade.live_order_id and self.us_live_mode:
            if hold_to_settlement:
                logger.info("Holding to US-platform settlement (exit_price={:.3f})", exit_price)
            else:
                if trade.us_market_slug:
                    self._us_clob.close_position(trade.us_market_slug)
                if trade.pnl_usd < 0:
                    self._us_clob.record_loss(abs(trade.pnl_usd))

        elif trade.clob_order_id and self.live_mode and trade.clob_token_id:
            if hold_to_settlement:
                logger.info(
                    "Holding to Polymarket settlement — token={} exit_price={:.3f}",
                    trade.clob_token_id[:12], exit_price,
                )
            else:
                self._clob.sell_order(trade.clob_token_id, exit_price, trade.shares)
                if trade.pnl_usd < 0:
                    self._clob.record_loss(abs(trade.pnl_usd))

        return trade

    def mark_to_market(self, opportunity_id: str, current_price: float) -> None:
        """Update unrealised P&L display value (does not close position)."""
        if opportunity_id in self.positions:
            # We don't mutate the trade object — just log the unrealised value
            trade = self.positions[opportunity_id]
            unrealised = (current_price - trade.entry_price) * trade.shares
            logger.debug(
                f"MTM {trade.question[:40]} | "
                f"entry={trade.entry_price:.3f} now={current_price:.3f} "
                f"unrealised=${unrealised:+.2f}"
            )

    # ─── Stats & display ──────────────────────────────────────────────────────

    def _starting_balance(self) -> float:
        # Paper book starting balance. trader.balance is the paper cash
        # ledger reconstructed from the JSONL trade log, so PnL must be
        # measured against the paper starting balance regardless of
        # whether a live CLOB client is also wired in. Live wallet PnL
        # is tracked separately via DashboardState.live_balance.
        return settings.simulated_starting_balance

    def total_pnl(self) -> float:
        return sum(t.pnl_usd for t in self.closed_trades)

    def win_rate(self) -> float:
        # Only count trades with a real outcome (exit != entry)
        decided = [t for t in self.closed_trades if t.exit_price is not None
                   and abs((t.exit_price or 0) - t.entry_price) > 0.001]
        if not decided:
            return 0.0
        wins = sum(1 for t in decided if (t.exit_price or 0) > t.entry_price)
        return wins / len(decided)

    def print_dashboard(self) -> None:
        console.rule("[bold cyan]📊 Paper Trading Dashboard")

        # Summary stats
        starting = self._starting_balance()
        nav = self.balance + sum(t.size_usd for t in self.positions.values())
        pnl = nav - starting

        console.print(
            f"  Balance: [green]${self.balance:.2f}[/]  |  "
            f"NAV: [cyan]${nav:.2f}[/]  |  "
            f"Total P&L: {'[green]' if pnl >= 0 else '[red]'}${pnl:+.2f}[/]  |  "
            f"Win rate: [yellow]{self.win_rate():.0%}[/]  |  "
            f"Closed trades: {len(self.closed_trades)}"
        )

        # Open positions table
        if self.positions:
            table = Table(title="Open Positions", box=box.SIMPLE_HEAVY)
            table.add_column("ID",       style="dim")
            table.add_column("Question", style="white", max_width=45)
            table.add_column("Side",     style="cyan")
            table.add_column("Entry",    style="yellow", justify="right")
            table.add_column("Size",     style="magenta", justify="right")
            table.add_column("Opened",   style="dim")

            for trade in self.positions.values():
                table.add_row(
                    trade.id,
                    trade.question[:44],
                    trade.side,
                    f"{trade.entry_price:.3f}",
                    f"${trade.size_usd:.2f}",
                    trade.opened_at.strftime("%m-%d %H:%M"),
                )
            console.print(table)

        # Recent closed trades
        if self.closed_trades:
            recent = self.closed_trades[-5:]
            table = Table(title="Recent Closed Trades", box=box.SIMPLE_HEAVY)
            table.add_column("ID",    style="dim")
            table.add_column("Question", style="white", max_width=40)
            table.add_column("Side",  style="cyan")
            table.add_column("Entry", justify="right")
            table.add_column("Exit",  justify="right")
            table.add_column("P&L",   justify="right")

            for trade in reversed(recent):
                pnl_color = "green" if trade.pnl_usd >= 0 else "red"
                table.add_row(
                    trade.id,
                    trade.question[:39],
                    trade.side,
                    f"{trade.entry_price:.3f}",
                    f"{trade.exit_price:.3f}" if trade.exit_price else "-",
                    f"[{pnl_color}]${trade.pnl_usd:+.2f}[/]",
                )
            console.print(table)

        console.rule()