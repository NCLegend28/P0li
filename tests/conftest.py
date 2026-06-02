"""Shared fixtures for all test modules."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from polybot.models import Market, Outcome, MarketCategory, TradeRecord, Side, TradeStatus


@pytest.fixture
def sample_market() -> Market:
    return Market(
        id="test-market-001",
        question="Will the highest temperature in Dallas be between 80-81°F on March 24?",
        category=MarketCategory.WEATHER,
        end_date=datetime.now(timezone.utc) + timedelta(hours=48),
        liquidity_usd=1000.0,
        volume_usd=5000.0,
        outcomes=[
            Outcome(name="Yes", price=0.35),
            Outcome(name="No",  price=0.65),
        ],
    )


@pytest.fixture
def open_trade(sample_market: Market) -> TradeRecord:
    return TradeRecord(
        opportunity_id="opp-001",
        market_id=sample_market.id,
        question=sample_market.question,
        side=Side.YES,
        entry_price=0.35,
        size_usd=10.0,
        shares=10.0 / 0.35,
        status=TradeStatus.OPEN,
    )
