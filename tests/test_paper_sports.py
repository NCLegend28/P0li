"""End-to-end paper-sports routing tests.

Scenario: LIVE_TRADING=true (global CLOB wired) but SPORTS_ALERT_ONLY=true
(US client NOT wired). Weather opportunities should hit the live CLOB; sports
opportunities must fall through to paper — never silently no-op.
"""
from __future__ import annotations

import pytest

from polybot.config import settings
from polybot.models import Market, MarketCategory, Opportunity, Outcome, Side
from polybot.trading import engine as engine_mod
from polybot.trading.engine import TradingEngine


class _FakeGlobalClob:
    """Stand-in for ClobClient — records orders so we can assert they fired."""
    _daily_loss = 0
    def __init__(self): self.orders = []
    def get_balance(self): return 1000.0
    def place_order(self, *, token_id, side, price, size_usd):
        self.orders.append({"token_id": token_id, "side": side, "price": price, "size_usd": size_usd})
        return "clob-order-id"
    def sell_order(self, *args, **kwargs): pass
    def record_loss(self, *args, **kwargs): pass


@pytest.fixture
def sports_market() -> Market:
    from datetime import datetime, timezone, timedelta
    return Market(
        id="sports-1", question="Lakers beat Celtics tonight?",
        category=MarketCategory.SPORTS,
        end_date=datetime.now(timezone.utc) + timedelta(hours=6),
        liquidity_usd=5000.0, volume_usd=10000.0,
        outcomes=[Outcome(name="Yes", price=0.55, clobTokenId="t-yes"),
                  Outcome(name="No",  price=0.45, clobTokenId="t-no")],
    )


@pytest.fixture
def weather_market() -> Market:
    from datetime import datetime, timezone, timedelta
    return Market(
        id="wx-1", question="Dallas hottest 80-81F tomorrow?",
        category=MarketCategory.WEATHER,
        end_date=datetime.now(timezone.utc) + timedelta(hours=24),
        liquidity_usd=1000.0, volume_usd=2000.0,
        outcomes=[Outcome(name="Yes", price=0.35, clobTokenId="wx-t-yes"),
                  Outcome(name="No",  price=0.65, clobTokenId="wx-t-no")],
    )


def _opp(market: Market, *, slug: str = "", market_price: float = 0.5, edge: float = 0.10) -> Opportunity:
    return Opportunity(
        market            = market,
        side              = Side.YES,
        market_price      = market_price,
        model_probability = market_price + edge,
        edge              = edge,
        strategy          = "weather_trader" if not slug else "sports_trader",
        confidence        = 0.7,
        us_market_slug    = slug,
        size_usd          = 20.0,
    )


@pytest.fixture(autouse=True)
def isolated_trade_log(tmp_path, monkeypatch):
    """CRITICAL: keep tests from writing into data/trades/trades.jsonl on disk."""
    monkeypatch.setattr(engine_mod, "TRADE_LOG_PATH", tmp_path / "trades.jsonl")


@pytest.fixture
def engine_live_weather_paper_sports(monkeypatch):
    """LIVE_TRADING=true, global CLOB wired, US client NOT wired."""
    monkeypatch.setattr(settings, "live_trading", True)
    e = TradingEngine()
    e.positions.clear()
    e.closed_trades.clear()
    e.balance = 1000.0
    e._live_starting_balance = 1000.0   # bypass set_clob_client logging
    e._clob = _FakeGlobalClob()
    # _us_clob stays None — this is the SPORTS_ALERT_ONLY=true scenario
    assert e.live_mode is True
    assert e.us_live_mode is False
    return e


class TestPerVenueRouting:
    def test_venue_helper_routes_sports_by_us_flag(self, engine_live_weather_paper_sports, sports_market, weather_market):
        e = engine_live_weather_paper_sports
        wx_opp = _opp(weather_market, market_price=0.35, edge=0.10)
        sp_opp = _opp(sports_market, slug="lakers-celtics", market_price=0.55, edge=0.08)
        assert e._venue_is_live(wx_opp) is True
        assert e._venue_is_live(sp_opp) is False

    def test_sports_opp_routes_to_paper_book(self, engine_live_weather_paper_sports, sports_market):
        e = engine_live_weather_paper_sports
        opp = _opp(sports_market, slug="lakers-celtics", market_price=0.55, edge=0.08)
        start = e.balance

        trade = e.open_position(opp)

        assert trade is not None
        assert trade.live_order_id is None, "paper sports must not record a live order id"
        assert trade.clob_order_id is None
        assert trade.live_platform == "polymarket_us", "venue marker required for sports exit pipeline"
        assert trade.us_market_slug == "lakers-celtics"
        # Paper book debited
        assert e.balance == pytest.approx(start - trade.size_usd)
        # No US order was placed (us_clob is None — would have crashed if attempted)
        assert opp.id in e.positions

    def test_weather_opp_still_goes_live(self, engine_live_weather_paper_sports, weather_market):
        e = engine_live_weather_paper_sports
        opp = _opp(weather_market, market_price=0.35, edge=0.10)
        start = e.balance

        trade = e.open_position(opp)

        assert trade is not None
        assert trade.clob_order_id == "clob-order-id", "weather must place a real CLOB order"
        assert trade.live_platform == "polymarket_global"
        # Live trades don't decrement the paper book
        assert e.balance == pytest.approx(start)
        assert len(e._clob.orders) == 1

    def test_close_paper_sports_credits_paper_book(self, engine_live_weather_paper_sports, sports_market):
        e = engine_live_weather_paper_sports
        opp = _opp(sports_market, slug="lakers-celtics", market_price=0.55, edge=0.08)
        trade = e.open_position(opp)
        before_close = e.balance

        closed = e.close_position(opp.id, exit_price=0.80)

        assert closed.pnl_usd > 0
        proceeds = 0.80 * trade.shares
        assert e.balance == pytest.approx(before_close + proceeds)
        # Should NOT have tried to call the unwired US client
        assert closed in e.closed_trades

    def test_sports_exit_pipeline_filter_finds_paper_trade(self, engine_live_weather_paper_sports, sports_market):
        """sports_graph.monitor_sports_positions filters by live_platform=='polymarket_us'.
        Paper sports must carry that marker so it can be exit-monitored."""
        e = engine_live_weather_paper_sports
        opp = _opp(sports_market, slug="lakers-celtics", market_price=0.55, edge=0.08)
        trade = e.open_position(opp)

        sports_positions = [t for t in e.positions.values() if t.live_platform == "polymarket_us"]
        assert len(sports_positions) == 1
        assert sports_positions[0].opportunity_id == opp.id
