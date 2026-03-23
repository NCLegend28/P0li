"""
Telegram bot interface.

Commands:
  /start     — welcome message
  /status    — current scanner state (running/paused, scan #, balance)
  /positions — all open paper positions with P&L
  /pnl       — closed trade summary
  /pause     — pause the scan loop
  /resume    — resume the scan loop
  /stop      — graceful shutdown

Architecture:
  The bot runs in its own asyncio task via Application.run_polling()
  wrapped in a background task. It communicates with the scan loop
  through a shared BotState object (a simple dataclass with asyncio.Event
  flags for pause/stop and a reference to the PaperTrader).

  The analogy: Telegram is a walkie-talkie clipped to your belt.
  The scan loop runs on the factory floor. You don't walk to the floor
  to check on things — you radio in.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

if TYPE_CHECKING:
    from polybot.paper.trader import PaperTrader


# ─── Shared state between scan loop and bot ───────────────────────────────────

@dataclass
class BotState:
    trader:        "PaperTrader | None" = None
    paused:        bool                 = False
    stop_event:    asyncio.Event        = field(default_factory=asyncio.Event)
    scan_number:   int                  = 0
    last_scan_at:  datetime | None      = None
    last_opps:     int                  = 0


# ─── Command handlers ─────────────────────────────────────────────────────────

def _make_handlers(state: BotState):
    """
    Returns all command handler functions bound to the shared BotState.
    Defined as closures so they share the state object without globals.
    """

    async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "🦞 *Polymarket Paper Bot*\n\n"
            "Commands:\n"
            "/status — scanner state\n"
            "/positions — open trades\n"
            "/pnl — closed trade summary\n"
            "/pause — pause scan loop\n"
            "/resume — resume scan loop\n"
            "/stop — shut down bot",
            parse_mode="Markdown",
        )

    async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        scanner_state = "⏸ PAUSED" if state.paused else "🟢 RUNNING"
        last = (
            state.last_scan_at.strftime("%H:%M:%S UTC")
            if state.last_scan_at else "never"
        )

        lines = [
            f"*Scanner:* {scanner_state}",
            f"*Scan #:* {state.scan_number}",
            f"*Last scan:* {last}",
            f"*Opportunities (last scan):* {state.last_opps}",
        ]

        if state.trader:
            t = state.trader
            nav = t.balance + sum(p.size_usd for p in t.positions.values())
            pnl = nav - t._starting_balance()
            lines += [
                "",
                f"*Paper balance:* ${t.balance:.2f}",
                f"*NAV:* ${nav:.2f}",
                f"*Total P&L:* ${pnl:+.2f}",
                f"*Open positions:* {len(t.positions)}",
                f"*Closed trades:* {len(t.closed_trades)}",
                f"*Win rate:* {t.win_rate():.0%}",
            ]

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not state.trader or not state.trader.positions:
            await update.message.reply_text("No open positions.")
            return

        lines = ["*Open Positions*\n"]
        for trade in state.trader.positions.values():
            lines.append(
                f"• `{trade.id}` {trade.side} @ {trade.entry_price:.3f}\n"
                f"  ${trade.size_usd:.2f} | {trade.question[:48]}\n"
                f"  Opened: {trade.opened_at.strftime('%m-%d %H:%M UTC')}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not state.trader:
            await update.message.reply_text("Trader not initialized.")
            return

        t = state.trader
        if not t.closed_trades:
            await update.message.reply_text("No closed trades yet.")
            return

        total_pnl = t.total_pnl()
        wins  = sum(1 for tr in t.closed_trades if tr.pnl_usd > 0)
        loses = len(t.closed_trades) - wins

        lines = [
            f"*Closed Trades Summary*\n",
            f"Total P&L: *${total_pnl:+.2f}*",
            f"Trades: {len(t.closed_trades)}  (✅ {wins}  ❌ {loses})",
            f"Win rate: {t.win_rate():.0%}\n",
            "*Recent:*",
        ]
        for tr in reversed(t.closed_trades[-5:]):
            emoji = "✅" if tr.pnl_usd >= 0 else "❌"
            lines.append(
                f"{emoji} {tr.side} {tr.question[:38]}\n"
                f"   ${tr.pnl_usd:+.2f} ({tr.pnl_pct:+.1f}%)"
            )

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        state.paused = True
        await update.message.reply_text("⏸ Scanner paused. Send /resume to restart.")
        logger.info("Scanner paused via Telegram")

    async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        state.paused = False
        await update.message.reply_text("▶️ Scanner resumed.")
        logger.info("Scanner resumed via Telegram")

    async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("🛑 Shutting down. Goodbye.")
        logger.info("Stop command received via Telegram")
        state.stop_event.set()

    return [
        CommandHandler("start",     cmd_start),
        CommandHandler("status",    cmd_status),
        CommandHandler("positions", cmd_positions),
        CommandHandler("pnl",       cmd_pnl),
        CommandHandler("pause",     cmd_pause),
        CommandHandler("resume",    cmd_resume),
        CommandHandler("stop",      cmd_stop),
    ]


# ─── Alert push functions (called by scan loop) ───────────────────────────────

class TelegramAlerter:
    """
    Plain-text alerts only. Telegram Markdown parsers choke on
    market question text (degrees, %, parentheses, dashes, etc).
    parse_mode=None is the safe choice for dynamic content.
    """

    def __init__(self, app, chat_id: int):
        self._app     = app
        self._chat_id = chat_id

    async def send(self, text: str) -> None:
        await self._app.bot.send_message(
            chat_id    = self._chat_id,
            text       = text,
            parse_mode = None,
        )

    async def alert_opportunity(self, opp) -> None:
        await self.send(
            f"\U0001f3af NEW OPPORTUNITY\n"
            f"{opp.market.question[:70]}\n"
            f"Side: {opp.side}  @  {opp.market_price:.3f}  Edge: {opp.edge_pct}\n"
            f"{opp.notes[:80]}"
        )

    async def alert_trade_opened(self, trade) -> None:
        await self.send(
            f"\U0001f4c2 OPENED [{trade.id}]\n"
            f"{trade.question[:65]}\n"
            f"{trade.side} @ {trade.entry_price:.3f}  ${trade.size_usd:.2f}"
        )

    async def alert_trade_closed(self, trade, reason: str) -> None:
        emoji = "\u2705" if trade.pnl_usd >= 0 else "\u274c"
        await self.send(
            f"{emoji} CLOSED [{trade.id}]  PnL: ${trade.pnl_usd:+.2f} ({trade.pnl_pct:+.1f}%)\n"
            f"{trade.question[:65]}\n"
            f"{trade.side}  Reason: {reason}"
        )

    async def alert_scan_summary(self, scan_n: int, opps: int, exits: int) -> None:
        await self.send(
            f"\U0001f4ca Scan #{scan_n}  Opened: {opps}  Closed: {exits}"
        )


# ─── Bot lifecycle ────────────────────────────────────────────────────────────

def build_bot(token: str, state: BotState) -> Application:
    app = Application.builder().token(token).build()
    for handler in _make_handlers(state):
        app.add_handler(handler)
    return app


async def run_bot_async(token: str, state: BotState) -> None:
    """
    Runs the Telegram polling loop as a background asyncio task.
    Stops cleanly when state.stop_event is set.
    """
    app = build_bot(token, state)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram bot polling started")

    # Block until stop signal
    await state.stop_event.wait()

    logger.info("Telegram bot shutting down...")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()