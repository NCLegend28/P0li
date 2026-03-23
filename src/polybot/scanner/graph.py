"""
Scanner graph — LangGraph StateGraph v3.

Full node pipeline:
  fetch_markets
    → filter_markets
      → fetch_forecasts
        → run_strategies
          → monitor_positions   ← evaluates exits on open trades
            → END

Open positions are injected via ScanState.open_positions by the CLI before
each graph.ainvoke() call — no module-level globals needed.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langgraph.graph import StateGraph, END
from loguru import logger

import httpx

from polybot.api.gamma import GammaClient
from polybot.api.openmeteo import CityForecast, OpenMeteoClient
from polybot.config import settings
from polybot.models import MarketCategory
from polybot.scanner.state import ScanState
from polybot.strategies.exit import ExitSignal, compute_exit_signals
from polybot.strategies.weather import evaluate_weather_markets, parse_question


# ─── Node: fetch_markets ──────────────────────────────────────────────────────

async def fetch_markets(state: ScanState) -> dict[str, Any]:
    logger.info("📡 Fetching markets from Gamma API...")
    async with GammaClient() as gamma:
        markets = await gamma.fetch_markets(
            limit         = 500,
            min_liquidity = settings.min_liquidity_usd,
        )
    cats: dict[str, int] = {}
    for m in markets:
        cats[m.category] = cats.get(m.category, 0) + 1
    logger.info(f"Fetched {len(markets)} | {dict(sorted(cats.items(), key=lambda x: -x[1]))}")
    return {"raw_markets": markets}


# ─── Node: filter_markets ─────────────────────────────────────────────────────

async def filter_markets(state: ScanState) -> dict[str, Any]:
    filtered = [
        m for m in state.raw_markets
        if m.hours_until_close >= 2.0
        and 0.07 <= m.yes_price <= 0.93
    ]
    logger.info(f"Filter: {len(state.raw_markets)} → {len(filtered)}")
    return {"filtered_markets": filtered}


# ─── Node: fetch_forecasts ────────────────────────────────────────────────────

async def fetch_forecasts(state: ScanState) -> dict[str, Any]:
    weather_markets = [
        m for m in state.filtered_markets
        if m.category == MarketCategory.WEATHER
    ]

    # Also need forecasts for any cities in open positions (for exit monitoring)
    all_weather_questions = list(weather_markets)
    for trade in state.open_positions:
        match = next((m for m in state.filtered_markets if m.id == trade.market_id), None)
        if match:
            all_weather_questions.append(match)

    city_dates: dict[str, str] = {}
    for m in all_weather_questions:
        wq = parse_question(m.question)
        if wq and wq.city not in city_dates:
            city_dates[wq.city] = wq.target_date

    if not city_dates:
        logger.info("No parseable weather cities")
        return {"forecast_cache": {}}

    logger.info(f"Fetching {len(city_dates)} forecasts concurrently: {list(city_dates.keys())}")

    _sem = asyncio.Semaphore(5)

    async def _fetch_one(city: str, td: str) -> tuple[str, CityForecast | None]:
        async with _sem:
            await asyncio.sleep(0.1)
            try:
                async with OpenMeteoClient() as meteo:
                    return city, await meteo.fetch_forecast(city, target_date=td)
            except Exception as exc:
                logger.warning(f"Forecast failed for {city}: {exc}")
                return city, None

    results = await asyncio.gather(*[_fetch_one(c, td) for c, td in city_dates.items()])

    forecast_cache: dict[str, CityForecast] = {
        city: fc for city, fc in results if fc is not None
    }

    logger.info(f"Cached {len(forecast_cache)}/{len(city_dates)} city forecasts")
    return {"forecast_cache": forecast_cache}


# ─── Node: run_strategies ─────────────────────────────────────────────────────

async def run_strategies(state: ScanState) -> dict[str, Any]:
    all_opps = []

    weather_markets = [
        m for m in state.filtered_markets
        if m.category == MarketCategory.WEATHER
    ]

    if weather_markets and state.forecast_cache:
        opps = evaluate_weather_markets(
            markets   = weather_markets,
            forecasts = state.forecast_cache,
            min_edge  = settings.min_edge_threshold,
        )
        all_opps.extend(opps)
        logger.info(f"Weather strategy → {len(opps)} opportunities")
    else:
        logger.info(
            f"Weather strategy → skipped "
            f"(markets={len(weather_markets)}, forecasts={len(state.forecast_cache)})"
        )

    return {"opportunities": all_opps}


# ─── Node: monitor_positions ──────────────────────────────────────────────────

async def monitor_positions(state: ScanState) -> dict[str, Any]:
    """
    For each open paper trade, check if an exit condition has been met.

    IMPORTANT: resolved/near-certain markets (price > 0.93 or < 0.07) are
    filtered OUT of state.filtered_markets. Those are exactly the markets
    we need to monitor for resolution. So we fetch their current prices
    directly from Gamma, bypassing scanner filters.
    """
    if not state.open_positions:
        logger.debug("No open positions to monitor")
        return {"exit_signals": []}

    # Start with prices from the current scan (for still-active markets)
    current_prices: dict[str, float] = {
        m.id: m.yes_price for m in state.filtered_markets
    }
    hours_to_close: dict[str, float] = {
        m.id: m.hours_until_close for m in state.filtered_markets
    }

    # For any open position NOT in the filtered set, fetch directly from Gamma
    missing_ids = [
        t.market_id for t in state.open_positions
        if t.market_id not in current_prices
    ]
    if missing_ids:
        logger.info(f"Fetching {len(missing_ids)} position prices direct from Gamma")
        async with GammaClient() as gamma:
            for mid in missing_ids:
                try:
                    market = await gamma.fetch_market_by_id(mid)
                    if market:
                        current_prices[mid]  = market.yes_price
                        hours_to_close[mid]  = market.hours_until_close
                        logger.debug(
                            f"Direct fetch {mid[:8]}: yes={market.yes_price:.3f} "
                            f"hours={market.hours_until_close:.1f}"
                        )
                except (httpx.HTTPStatusError, httpx.RequestError) as e:
                    logger.warning(f"Could not fetch market {mid}: {e}")

    signals: list[ExitSignal] = compute_exit_signals(
        open_trades    = state.open_positions,
        current_prices = current_prices,
        hours_to_close = hours_to_close,
    )

    if signals:
        logger.info(f"Exit signals generated: {len(signals)}")
        for s in signals:
            logger.info(f"  [{s.reason}] trade={s.trade_id} exit_price={s.exit_price:.3f} | {s.note}")
    else:
        logger.debug(f"Monitoring {len(state.open_positions)} positions — all hold")

    return {"exit_signals": signals}


# ─── Graph assembly ───────────────────────────────────────────────────────────

def build_scanner_graph() -> Any:
    """
    Pipeline:
      fetch_markets → filter_markets → fetch_forecasts
        → run_strategies → monitor_positions → END
    """
    builder = StateGraph(ScanState)

    builder.add_node("fetch_markets",      fetch_markets)
    builder.add_node("filter_markets",     filter_markets)
    builder.add_node("fetch_forecasts",    fetch_forecasts)
    builder.add_node("run_strategies",     run_strategies)
    builder.add_node("monitor_positions",  monitor_positions)

    builder.set_entry_point("fetch_markets")
    builder.add_edge("fetch_markets",     "filter_markets")
    builder.add_edge("filter_markets",    "fetch_forecasts")
    builder.add_edge("fetch_forecasts",   "run_strategies")
    builder.add_edge("run_strategies",    "monitor_positions")
    builder.add_edge("monitor_positions", END)

    return builder.compile()
