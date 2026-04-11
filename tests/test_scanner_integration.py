"""
Integration stubs for the LangGraph scanner pipeline.

These tests mock the Gamma API client and verify that the pipeline
runs end-to-end without raising and that state flows correctly.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from polybot.scanner.graph import build_scanner_graph
from polybot.scanner.state import ScanState


@pytest.mark.asyncio
async def test_scanner_pipeline_empty_markets():
    """Full pipeline run with empty Gamma response — should not raise."""
    with patch("polybot.scanner.graph.GammaClient") as MockGamma:
        mock_client = AsyncMock()
        mock_client.fetch_markets = AsyncMock(return_value=[])
        MockGamma.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockGamma.return_value.__aexit__ = AsyncMock(return_value=None)

        graph = build_scanner_graph()
        result = await graph.ainvoke(ScanState(scan_number=1))

        assert result is not None
        opps = result.get("opportunities", []) if isinstance(result, dict) else result.opportunities
        assert opps == []


@pytest.mark.asyncio
async def test_scanner_open_positions_flow_through():
    """open_positions injected into ScanState flows to monitor_positions node."""
    from datetime import datetime, timezone, timedelta
    from polybot.models import Market, Outcome, MarketCategory, TradeRecord, Side, TradeStatus

    trade = TradeRecord(
        opportunity_id="opp-test",
        market_id="mkt-test",
        question="Will the highest temperature in Dallas be between 80-81°F on March 24?",
        side=Side.YES,
        entry_price=0.4,
        size_usd=10.0,
        shares=25.0,
        status=TradeStatus.OPEN,
    )

    initial_state = ScanState(scan_number=1, open_positions=[trade])

    with patch("polybot.scanner.graph.GammaClient") as MockGamma:
        mock_client = AsyncMock()
        mock_client.fetch_markets = AsyncMock(return_value=[])
        mock_client.fetch_market_by_id = AsyncMock(return_value=None)
        MockGamma.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockGamma.return_value.__aexit__ = AsyncMock(return_value=None)

        graph = build_scanner_graph()
        result = await graph.ainvoke(initial_state)

        # Pipeline ran without error; missing market should produce a MARKET_CLOSED signal
        assert result is not None
        signals = (
            result.get("exit_signals", [])
            if isinstance(result, dict)
            else result.exit_signals
        )
        assert len(signals) == 1
        assert signals[0].trade_id == trade.id


@pytest.mark.asyncio
async def test_scan_state_forecast_cache_empty_when_no_weather_markets():
    """fetch_forecasts returns empty cache when no weather markets exist."""
    with patch("polybot.scanner.graph.GammaClient") as MockGamma:
        mock_client = AsyncMock()
        mock_client.fetch_markets = AsyncMock(return_value=[])
        MockGamma.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockGamma.return_value.__aexit__ = AsyncMock(return_value=None)

        graph = build_scanner_graph()
        result = await graph.ainvoke(ScanState(scan_number=1))

        cache = (
            result.get("forecast_cache", {})
            if isinstance(result, dict)
            else result.forecast_cache
        )
        assert cache == {}
