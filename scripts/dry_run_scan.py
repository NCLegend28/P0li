"""One-shot dry run of the scanner pipeline.

Hits live Polymarket Gamma + Open-Meteo APIs, runs the full LangGraph pipeline once,
and prints what would happen — no orders, no state writes, no daemon loop.

Verifies:
  - Gamma API reachable, market discovery works
  - Filter narrows markets sanely
  - Open-Meteo forecasts return (with the new cache)
  - Strategies emit opportunities
  - Kelly + confidence sizing produces sensible bet sizes
"""
from __future__ import annotations

import asyncio
import time

from loguru import logger

from polybot.models import Opportunity
from polybot.scanner.graph import build_scanner_graph
from polybot.scanner.state import ScanState
from polybot.trading.engine import TradingEngine


def fmt_opp(opp: Opportunity, engine: TradingEngine) -> str:
    size = engine._size_position(opp)
    return (
        f"  [{opp.strategy:>16s}] {opp.side:<3s} @ {opp.market_price:.3f}  "
        f"model={opp.model_probability:.3f}  edge={opp.edge:+.3f}  "
        f"conf={opp.confidence:.2f}  → ${size:>5.2f}  | {opp.market.question[:60]}"
    )


async def main() -> None:
    print("=" * 78)
    print("DRY-RUN SCAN — no orders submitted, no state written")
    print("=" * 78)

    engine = TradingEngine()
    print(f"\nBankroll: ${engine.balance:,.2f}  (paper, {len(engine.positions)} open positions)")

    graph = build_scanner_graph()
    t0 = time.monotonic()
    result = await graph.ainvoke(ScanState(
        scan_number    = 0,
        open_positions = list(engine.positions.values()),
    ))
    dur = time.monotonic() - t0

    raw      = result.get("raw_markets", [])
    filtered = result.get("filtered_markets", [])
    opps     = result.get("opportunities", [])

    print(f"\nScan duration: {dur:.1f}s")
    print(f"  raw markets:      {len(raw)}")
    print(f"  filtered markets: {len(filtered)}")
    print(f"  opportunities:    {len(opps)}")

    cats: dict[str, int] = {}
    for m in raw:
        cats[str(m.category)] = cats.get(str(m.category), 0) + 1
    if cats:
        print(f"  categories: {dict(sorted(cats.items(), key=lambda x: -x[1]))}")

    if not opps:
        print("\nNo opportunities found this scan — strategies emitted nothing.")
        return

    print(f"\nTop {min(len(opps), 15)} opportunities by edge:")
    for opp in sorted(opps, key=lambda o: -o.edge)[:15]:
        print(fmt_opp(opp, engine))

    sizes = [engine._size_position(o) for o in opps]
    sizes = [s for s in sizes if s > 0]
    if sizes:
        print(f"\nSizing summary:  n={len(sizes)}  min=${min(sizes):.2f}  "
              f"max=${max(sizes):.2f}  total=${sum(sizes):.2f}  "
              f"(of ${engine.balance:.2f} bankroll, "
              f"40% exposure budget = ${engine.balance * 0.40:.2f})")


if __name__ == "__main__":
    logger.disable("polybot.utils.retry")
    asyncio.run(main())
