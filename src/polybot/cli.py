"""
polybot — main entry point.

Three concurrent asyncio tasks:
  1. scan_loop      — LangGraph pipeline every N seconds
  2. render_loop    — dashboard refresh every 0.5s
  3. telegram       — command polling (optional)

All output goes through Dashboard.log() — nothing prints to stdout directly.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from datetime import datetime, timezone

from loguru import logger

from polybot.config import settings
from polybot.paper.trader import PaperTrader
from polybot.scanner.graph import build_scanner_graph
from polybot.scanner.state import ScanState
from polybot.telegram.bot import BotState, TelegramAlerter, run_bot_async
from polybot.ui.dashboard import Dashboard, DashboardState
from polybot.web.server import run_server, set_dashboard_state


# ─── Logging — pipe to file only; terminal output owned by Rich Live ──────────

def _configure_logging() -> None:
    logger.remove()
    settings.log_file_path.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(settings.log_file_path),
        level    = settings.log_level,
        rotation = "10 MB",
        retention= "7 days",
        format   = "{time:HH:mm:ss} | {level: <8} | {message}",
    )


def _extract(result, field: str, default):
    if isinstance(result, dict):
        return result.get(field, default)
    return getattr(result, field, default)


# ─── Scan loop ────────────────────────────────────────────────────────────────

async def scan_loop(
    trader:    PaperTrader,
    dash:      Dashboard,
    ds:        DashboardState,
    bot_state: BotState,
    alerter:   TelegramAlerter | None,
) -> None:

    graph  = build_scanner_graph()
    scan_n = 0

    while not bot_state.stop_event.is_set():

        if bot_state.paused:
            ds.is_paused = True
            await asyncio.sleep(2)
            continue

        ds.is_paused = False
        scan_n += 1
        ds.scan_number  = scan_n
        ds.last_scan_at = datetime.now(timezone.utc)
        bot_state.scan_number  = scan_n
        bot_state.last_scan_at = ds.last_scan_at

        dash.log(f"Scan [bold cyan]#{scan_n}[/] started", "INFO")
        t0 = time.monotonic()

        # ── Run pipeline (open positions injected via state) ──────────────────
        result = await graph.ainvoke(
            ScanState(scan_number=scan_n, open_positions=list(trader.positions.values()))
        )

        ds.scan_duration = round(time.monotonic() - t0, 1)

        opps         = _extract(result, "opportunities", [])
        exit_signals = _extract(result, "exit_signals",  [])
        filtered     = _extract(result, "filtered_markets", [])
        raw          = _extract(result, "raw_markets", [])

        # ── Update dashboard state from scan results ───────────────────────────
        ds.opportunities    = opps
        ds.total_markets    = len(raw)
        ds.weather_mkts     = sum(1 for m in raw if m.category == "weather")
        ds.crypto_mkts      = sum(1 for m in raw if m.category == "crypto")
        ds.politics_mkts    = sum(1 for m in raw if m.category == "politics")
        ds.sports_mkts      = sum(1 for m in raw if m.category == "sports")
        ds.other_mkts       = sum(1 for m in raw if m.category == "other")
        ds.forecasts_fetched= len(set(
            o.notes.split()[0] for o in opps if o.notes
        ))

        # Weather market feed for right panel
        # Build feed from ALL raw weather markets (not just filtered)
        raw_weather = [m for m in raw if m.category == "weather"]
        feed_ids    = {m.id for m in raw_weather}

        # For open positions whose markets are NOT in the scan batch
        # (they resolved / closed and were dropped by Gamma active-only query),
        # fetch their current price directly so NOW / UNREAL stay populated.
        missing_ids = [
            t.market_id for t in trader.positions.values()
            if t.market_id not in feed_ids
        ]
        extra_markets = []
        if missing_ids:
            import httpx
            from polybot.api.gamma import GammaClient
            async with GammaClient() as gamma:
                for mid in missing_ids:
                    try:
                        m = await gamma.fetch_market_by_id(mid)
                        if m:
                            extra_markets.append(m)
                    except (httpx.HTTPStatusError, httpx.RequestError) as e:
                        logger.warning(f"Could not fetch market {mid} for dashboard: {e}")

        ds.market_feed = [
            {
                "id":               m.id,
                "question":         m.question,
                "yes_price":        m.yes_price,
                "liquidity_usd":    m.liquidity_usd,
                "hours_until_close":m.hours_until_close,
            }
            for m in raw_weather + extra_markets
        ]

        # Record sparkline history
        nav = trader.balance + sum(t.size_usd for t in trader.positions.values())
        ds.record_scan(ds.scan_duration, nav)

        # Best edge seen today
        if opps:
            best = max(o.edge for o in opps)
            if best > ds.best_edge_today:
                ds.best_edge_today = best

        dash.log(
            f"Scanned [{settings.min_liquidity_usd:.0f}$ min liq] — "
            f"[cyan]{len(raw)}[/] raw → [cyan]{len(filtered)}[/] filtered — "
            f"[yellow]{len(opps)}[/] opps — took [dim]{ds.scan_duration}s[/]",
            "INFO",
        )

        # ── Execute exits ──────────────────────────────────────────────────────
        exit_count = 0
        for signal in exit_signals:
            opp_id = next(
                (k for k, t in trader.positions.items() if t.id == signal.trade_id),
                None,
            )
            if opp_id is None:
                continue

            closed = await trader.close_position(opp_id, signal.exit_price)
            exit_count += 1
            ds.daily_trades_closed += 1
            ds.daily_pnl += closed.pnl_usd
            pnl_sign = "+" if closed.pnl_usd >= 0 else ""
            dash.log(
                f"[EXIT] [magenta]{closed.id}[/]  {closed.side} → "
                f"[{'green' if closed.pnl_usd >= 0 else 'red'}]"
                f"{pnl_sign}${closed.pnl_usd:.2f}[/]  "
                f"[dim]{signal.reason}[/]",
                "EXIT",
            )
            if alerter:
                await alerter.alert_trade_closed(closed, signal.reason)

        # ── Open new positions ─────────────────────────────────────────────────
        open_count = 0
        for opp in opps:
            already = any(t.market_id == opp.market.id for t in trader.positions.values())
            if already:
                continue

            trade = await trader.open_position(opp)
            if trade:
                open_count += 1
                ds.daily_trades_opened += 1
                dash.log(
                    f"[OPEN] [cyan]{trade.id}[/]  "
                    f"[{'green' if trade.side == 'YES' else 'red'}]{trade.side}[/] "
                    f"@ [yellow]{trade.entry_price:.3f}[/]  "
                    f"edge=[cyan]{opp.edge_pct}[/]  "
                    f"[dim]{opp.market.question[:42]}[/]",
                    "TRADE",
                )
                if alerter:
                    await alerter.alert_opportunity(opp)
                    await alerter.alert_trade_opened(trade)

        if open_count == 0 and exit_count == 0:
            dash.log("No actions this scan — all positions held", "INFO")

        bot_state.last_opps = len(opps)

        if alerter and (open_count > 0 or exit_count > 0):
            await alerter.alert_scan_summary(scan_n, open_count, exit_count)

        # ── Countdown sleep ────────────────────────────────────────────────────
        sleep_total = settings.scan_interval_seconds
        elapsed     = 0.0
        while elapsed < sleep_total and not bot_state.stop_event.is_set():
            ds.next_scan_in = max(0.0, sleep_total - elapsed)
            await asyncio.sleep(1.0)
            elapsed += 1.0

    ds.is_running = False
    dash.log("Scan loop stopped.", "WARN")


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    _configure_logging()

    trader    = PaperTrader()
    ds        = DashboardState(
        trader        = trader,
        scan_interval = settings.scan_interval_seconds,
    )
    bot_state = BotState(trader=trader)

    # ── Graceful shutdown on SIGTERM / SIGINT ─────────────────────────────────
    loop = asyncio.get_running_loop()

    def _handle_signal() -> None:
        logger.warning("Shutdown signal received — stopping scan loop")
        bot_state.stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal)

    # ── Telegram ──────────────────────────────────────────────────────────────
    tg_token   = os.getenv("TELEGRAM_BOT_TOKEN", settings.telegram_bot_token)
    tg_chat_id = int(os.getenv("TELEGRAM_CHAT_ID", str(settings.telegram_chat_id)))
    alerter: TelegramAlerter | None = None
    tasks: list[asyncio.Task] = []

    # ── Web dashboard ─────────────────────────────────────────────────────────
    set_dashboard_state(ds)
    if settings.web_enabled:
        tasks.append(asyncio.create_task(
            run_server(settings.web_host, settings.web_port), name="web"
        ))

    with Dashboard(ds) as dash:
        dash.log("Polymarket Bot starting up...", "INFO")
        dash.log(
            f"Config: interval=[cyan]{settings.scan_interval_seconds}s[/]  "
            f"min_liq=[cyan]${settings.min_liquidity_usd:.0f}[/]  "
            f"min_edge=[cyan]{settings.min_edge_threshold:.0%}[/]  "
            f"max_pos=[cyan]${settings.paper_max_position_usd:.0f}[/]",
            "INFO",
        )

        if tg_token and tg_chat_id:
            from polybot.telegram.bot import build_bot
            tg_app = build_bot(tg_token, bot_state)
            alerter = TelegramAlerter(tg_app, tg_chat_id)

            async def _tg_task():
                """Telegram wrapper: retries on timeout, never crashes the bot."""
                import telegram.error as tg_err
                retry = 0
                while not bot_state.stop_event.is_set():
                    try:
                        await run_bot_async(tg_token, bot_state)
                        break  # clean stop
                    except (tg_err.TimedOut, tg_err.NetworkError, OSError) as e:
                        retry += 1
                        wait = min(30, 5 * retry)
                        dash.log(f"Telegram timeout ({e.__class__.__name__}), retry in {wait}s", "WARN")
                        await asyncio.sleep(wait)
                    except asyncio.CancelledError:
                        break

            tasks.append(asyncio.create_task(_tg_task(), name="telegram"))
            dash.log("Telegram bot starting...", "INFO")
        else:
            dash.log(
                "Telegram not configured — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID",
                "WARN",
            )

        # Renderer task
        tasks.append(asyncio.create_task(
            dash.run_renderer(), name="renderer"
        ))

        # Main scan loop
        tasks.append(asyncio.create_task(
            scan_loop(trader, dash, ds, bot_state, alerter), name="scanner"
        ))

        # Only the scanner task stopping should end the bot.
        # Web, telegram, and renderer failures are logged but non-fatal.
        scanner_task = next(t for t in tasks if t.get_name() == "scanner")
        await asyncio.wait([scanner_task])
        done, pending = {scanner_task}, set(t for t in tasks if t is not scanner_task)

        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # Dashboard context exited — safe to print again
    print("\n\033[1;32mPolybot stopped cleanly.\033[0m")


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()