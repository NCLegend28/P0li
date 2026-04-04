"""
Paper trading engine.

The analogy: this is a flight simulator. Same controls, same instruments,
same cockpit — but the plane never leaves the ground. You build muscle memory
before touching real capital.

Responsibilities:
  - Maintain a virtual balance
  - Open/close simulated positions
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
from polybot.models import Opportunity, PaperTrade, Side, TradeStatus

TRADE_LOG_PATH = Path("data/trades/paper_trades.jsonl")
console = Console()


class PaperTrader:
    def __init__(self):
        self.balance:      float             = settings.paper_starting_balance
        self.positions:    dict[str, PaperTrade] = {}   # opportunity_id → trade
        self.closed_trades: list[PaperTrade] = []
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

        with TRADE_LOG_PATH.open() as f:
            for line in f:
                raw = json.loads(line.strip())
                trade = PaperTrade.model_validate(raw)
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

    def _append_trade(self, trade: PaperTrade) -> None:
        with TRADE_LOG_PATH.open("a") as f:
            f.write(trade.model_dump_json() + "\n")

    # ─── Position management ──────────────────────────────────────────────────

    def open_position(self, opp: Opportunity) -> PaperTrade | None:
        if len(self.positions) >= settings.max_open_positions:
            logger.debug(f"Max positions reached ({settings.max_open_positions}), skipping")
            return None

        if opp.id in self.positions:
            logger.debug(f"Already have position for opportunity {opp.id}")
            return None

        max_pos = settings.live_max_position_usd if (self.live_mode or self.us_live_mode) else settings.paper_max_position_usd
        size_usd = min(max_pos, self.balance * 0.1)
        if size_usd < 1.0:
            logger.warning("Balance too low to open new position")
            return None

        shares   = size_usd / opp.market_price
        is_sports = bool(opp.us_market_slug)

        # ── Live mode — only execute real orders, no paper simulation ─────────
        if self.live_mode or self.us_live_mode:
            trade = PaperTrade(
                opportunity_id = opp.id,
                market_id      = opp.market.id,
                question       = opp.market.question,
                side           = opp.side,
                entry_price    = opp.market_price,
                size_usd       = size_usd,
                shares         = shares,
                live_platform  = "polymarket_us" if is_sports else "polymarket_global",
            )

            if is_sports and self.us_live_mode:
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
                order_id = order.get("id")
                trade = trade.model_copy(update={"live_order_id": order_id, "us_market_slug": opp.us_market_slug})
                logger.success(
                    "LIVE ORDER PLACED (US) | {} {} @ {:.3f} | ${:.2f} | order_id={}",
                    trade.side, trade.question[:45], trade.entry_price, size_usd, order_id,
                )

            elif not is_sports and self.live_mode:
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

            # Only track position after confirmed live fill
            self.positions[opp.id] = trade
            self._append_trade(trade)
            return trade

        # ── Paper mode ────────────────────────────────────────────────────────
        trade = PaperTrade(
            opportunity_id = opp.id,
            market_id      = opp.market.id,
            question       = opp.market.question,
            side           = opp.side,
            entry_price    = opp.market_price,
            size_usd       = size_usd,
            shares         = shares,
        )

        self.positions[opp.id] = trade
        self.balance -= size_usd
        self._append_trade(trade)

        logger.info(
            f"OPEN  {trade.side} {trade.question[:45]}... "
            f"@ {trade.entry_price:.3f} | ${size_usd:.2f} | "
            f"opp_id={opp.id}"
        )
        return trade

    def close_position(self, opportunity_id: str, exit_price: float) -> PaperTrade:
        trade = self.positions.pop(opportunity_id)

        trade = trade.model_copy(update={
            "status":     TradeStatus.CLOSED,
            "exit_price": exit_price,
            "closed_at":  datetime.now(timezone.utc),
        })

        # Only adjust paper balance in paper mode — live balance is synced from CLOB
        if not (self.live_mode or self.us_live_mode):
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
        if trade.live_platform == "polymarket_us" and self.us_live_mode:
            if trade.us_market_slug:
                self._us_clob.close_position(trade.us_market_slug)
            if trade.pnl_usd < 0:
                self._us_clob.record_loss(abs(trade.pnl_usd))

        elif self.live_mode and trade.clob_token_id:
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
        if self.live_mode and self._live_starting_balance is not None:
            return self._live_starting_balance
        return settings.paper_starting_balance

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