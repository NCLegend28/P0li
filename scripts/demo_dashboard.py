"""
scripts/demo_dashboard.py

Runs the live dashboard with simulated data for 30 seconds.
Use this to preview the UI without running a real scan.

Usage:
    cd polymarket-bot
    PYTHONPATH=src python scripts/demo_dashboard.py
"""

import asyncio
import math
import random
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "src")

from polybot.models import Market, Outcome, Opportunity, Side, MarketCategory
from polybot.paper.trader import PaperTrader
from polybot.ui.dashboard import Dashboard, DashboardState


def fake_market(qn: str, yes_price: float = 0.3, hours: float = 24.0, liq: float = 1200) -> Market:
    end = datetime.now(timezone.utc) + timedelta(hours=hours)
    return Market(
        id=qn[:8].replace(" ", "_"),
        question=qn,
        category=MarketCategory.WEATHER,
        end_date=end,
        liquidity_usd=liq,
        volume_usd=liq * 2,
        outcomes=[
            Outcome(name="Yes", price=yes_price),
            Outcome(name="No",  price=round(1 - yes_price, 4)),
        ],
    )


MARKETS = [
    fake_market("Will Chicago high temp be 63°F or below on March 22?",       0.565, 15.8, 984),
    fake_market("Will Singapore highest temp be 33°C on March 25?",            0.355, 87.8, 771),
    fake_market("Will Miami highest temp be between 82-83°F on March 23?",     0.280, 39.8, 774),
    fake_market("Will Seoul highest temp be 13°C on March 24?",                0.345, 60.1, 920),
    fake_market("Will Wellington highest temp be 20°C on March 25?",           0.230, 88.2, 512),
    fake_market("Will Dallas highest temp be between 86-87°F on March 24?",    0.185, 48.3, 1426),
    fake_market("Will New York City highest temp be 66-67°F on March 24?",     0.170, 72.0, 1887),
    fake_market("Will Buenos Aires highest temp be 30°C on March 24?",         0.016, 60.0, 2122),
]

OPPS = [
    Opportunity(market=MARKETS[0], side=Side.YES, market_price=0.565,
                model_probability=0.94, edge=0.3755, strategy="weather_trader",
                notes="CHICAGO high=14.3°C (57.7°F) | bracket=[-999.0,17.2]°C"),
    Opportunity(market=MARKETS[1], side=Side.NO,  market_price=0.645,
                model_probability=0.076, edge=0.2792, strategy="weather_trader",
                notes="SINGAPORE high=35.5°C (95.9°F) | bracket=[32.5,33.5]°C"),
    Opportunity(market=MARKETS[2], side=Side.NO,  market_price=0.720,
                model_probability=0.027, edge=0.2485, strategy="weather_trader",
                notes="MIAMI high=25.0°C (77.0°F) | bracket=[27.8,28.3]°C"),
    Opportunity(market=MARKETS[3], side=Side.NO,  market_price=0.655,
                model_probability=0.132, edge=0.2127, strategy="weather_trader",
                notes="SEOUL high=14.7°C (58.5°F) | bracket=[12.5,13.5]°C"),
    Opportunity(market=MARKETS[4], side=Side.NO,  market_price=0.770,
                model_probability=0.055, edge=0.1748, strategy="weather_trader",
                notes="WELLINGTON high=17.1°C (62.8°F) | bracket=[19.5,20.5]°C"),
]


async def simulate(ds: DashboardState, trader: PaperTrader, dash: Dashboard) -> None:
    scan = 0

    # Pre-open two positions
    trader.open_position(OPPS[0])
    trader.open_position(OPPS[1])
    dash.log("Loaded existing paper positions from trade log", "INFO")
    dash.log("Config: interval=120s  min_liq=$500  min_edge=8%  max_pos=$10", "INFO")
    dash.log("Telegram not configured — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID", "WARN")

    # Update market feed
    ds.market_feed = [
        {"id": m.id, "question": m.question, "yes_price": m.yes_price,
         "liquidity_usd": m.liquidity_usd, "hours_until_close": m.hours_until_close}
        for m in MARKETS
    ]

    await asyncio.sleep(3)

    while ds.is_running:
        scan += 1
        ds.scan_number   = scan
        ds.last_scan_at  = datetime.now(timezone.utc)
        ds.scan_duration = round(1.8 + random.uniform(-0.3, 0.8), 1)
        ds.total_markets = 389
        ds.weather_mkts  = 32
        ds.crypto_mkts   = 50
        ds.politics_mkts = 43
        ds.sports_mkts   = 20
        ds.other_mkts    = 244
        ds.forecasts_fetched = 9
        ds.opportunities = OPPS[:5]

        # Jitter prices in market feed
        for m in ds.market_feed:
            drift = random.uniform(-0.008, 0.008)
            m["yes_price"] = round(max(0.02, min(0.97, m["yes_price"] + drift)), 3)

        dash.log(f"Scan [bold cyan]#{scan}[/] — [cyan]389[/] raw → [cyan]198[/] filtered — [yellow]{len(OPPS)}[/] opps — took [dim]{ds.scan_duration}s[/]", "INFO")

        # Simulate opening a new position on scan 2
        if scan == 2 and len(trader.positions) < 4:
            opp = OPPS[2]
            trade = trader.open_position(opp)
            if trade:
                dash.log(
                    f"[OPEN] [cyan]{trade.id}[/]  [red]NO[/] @ [yellow]{trade.entry_price:.3f}[/]  "
                    f"edge=[cyan]{opp.edge_pct}[/]  [dim]{opp.market.question[:42]}[/]",
                    "TRADE",
                )

        # Simulate profit-target exit on scan 4
        if scan == 4 and trader.positions:
            opp_ids = list(trader.positions.keys())
            if opp_ids:
                opp_id = opp_ids[0]
                trade  = trader.positions[opp_id]
                fake_exit = round(min(trade.entry_price * 1.85, 0.91), 3)
                closed = trader.close_position(opp_id, fake_exit)
                dash.log(
                    f"[EXIT] [magenta]{closed.id}[/]  {closed.side} → "
                    f"[green]+${closed.pnl_usd:.2f}[/]  [dim]profit_target[/]",
                    "EXIT",
                )

        # Simulate edge_collapse on scan 6
        if scan == 6 and trader.positions:
            opp_ids = list(trader.positions.keys())
            if opp_ids:
                opp_id = opp_ids[0]
                trade  = trader.positions[opp_id]
                fake_exit = round(trade.entry_price * 0.92, 3)
                closed = trader.close_position(opp_id, fake_exit)
                dash.log(
                    f"[EXIT] [magenta]{closed.id}[/]  {closed.side} → "
                    f"[red]${closed.pnl_usd:.2f}[/]  [dim]edge_collapsed[/]",
                    "EXIT",
                )

        if scan >= 12:
            ds.is_running = False
            dash.log("Demo complete. Run [cyan]PYTHONPATH=src python -m polybot.cli[/] for real mode.", "WARN")
            break

        # Countdown
        for i in range(8, 0, -1):
            ds.next_scan_in = float(i)
            await asyncio.sleep(0.5)


async def main() -> None:
    trader = PaperTrader()
    ds     = DashboardState(
        trader        = trader,
        scan_interval = 120,
        next_scan_in  = 8.0,
        is_running    = True,
    )

    with Dashboard(ds) as dash:
        await asyncio.gather(
            simulate(ds, trader, dash),
            dash.run_renderer(),
        )

    print("\n\033[1;32mDemo finished.\033[0m")
    print("Run the real bot:  PYTHONPATH=src python -m polybot.cli")


if __name__ == "__main__":
    asyncio.run(main())
