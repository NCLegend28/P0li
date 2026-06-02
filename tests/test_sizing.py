"""Unit tests for TradingEngine._size_position — Kelly + confidence sizing."""
from __future__ import annotations

import pytest

from polybot.config import settings
from polybot.models import Market, Opportunity, Side
from polybot.trading import engine as engine_mod
from polybot.trading.engine import TradingEngine


@pytest.fixture(autouse=True)
def isolated_trade_log(tmp_path, monkeypatch):
    """CRITICAL: keep TradingEngine() from reading or writing the real trade log."""
    monkeypatch.setattr(engine_mod, "TRADE_LOG_PATH", tmp_path / "trades.jsonl")


def _opp(
    market: Market,
    *,
    market_price: float,
    edge: float,
    confidence: float = 1.0,
    size_usd: float = 10.0,
) -> Opportunity:
    return Opportunity(
        market            = market,
        side              = Side.YES,
        market_price      = market_price,
        model_probability = market_price + edge,
        edge              = edge,
        strategy          = "test",
        confidence        = confidence,
        size_usd          = size_usd,
    )


class TestKellySizing:
    def test_zero_edge_yields_zero(self, sample_market):
        engine = TradingEngine()
        engine.balance = 1000.0
        size = engine._size_position(_opp(sample_market, market_price=0.50, edge=0.0))
        assert size == 0.0

    def test_negative_edge_yields_zero(self, sample_market):
        engine = TradingEngine()
        engine.balance = 1000.0
        size = engine._size_position(_opp(sample_market, market_price=0.50, edge=-0.05))
        assert size == 0.0

    def test_degenerate_price_yields_zero(self, sample_market):
        engine = TradingEngine()
        engine.balance = 1000.0
        assert engine._size_position(_opp(sample_market, market_price=0.0,  edge=0.10)) == 0.0
        assert engine._size_position(_opp(sample_market, market_price=1.0,  edge=0.10)) == 0.0

    def test_quarter_kelly_at_full_confidence(self, sample_market):
        # full Kelly = 0.10 / (1 - 0.50) = 0.20 → quarter = 0.05 → bet $50 of $1000
        # capped by simulated_max_position_usd (default 10) and size_usd cap (default 10)
        engine = TradingEngine()
        engine.balance = 1000.0
        opp = _opp(sample_market, market_price=0.50, edge=0.10, confidence=1.0, size_usd=100.0)
        size = engine._size_position(opp)
        # raw=$50, global_cap=10 (paper), strategy_cap=100, exposure_budget=400 → min = 10
        assert size == pytest.approx(settings.simulated_max_position_usd)

    def test_confidence_scales_size(self, sample_market):
        # With a high strategy_cap and a small bankroll where Kelly is the binding constraint.
        # full_kelly = 0.04 / (1 - 0.50) = 0.08 → quarter @ conf=1.0 = 0.02 → $2 of $100
        # quarter @ conf=0.5 = 0.01 → $1 of $100. Halving confidence halves the bet.
        engine = TradingEngine()
        engine.positions.clear()   # don't inherit positions from the real trade log on disk
        engine.balance = 100.0
        full = engine._size_position(_opp(sample_market, market_price=0.50, edge=0.04, confidence=1.0, size_usd=1000.0))
        half = engine._size_position(_opp(sample_market, market_price=0.50, edge=0.04, confidence=0.5, size_usd=1000.0))
        assert full == pytest.approx(2.0, abs=0.01)
        assert half == pytest.approx(1.0, abs=0.01)

    def test_strategy_cap_binds(self, sample_market):
        # Kelly would suggest more than the strategy-supplied cap.
        engine = TradingEngine()
        engine.balance = 1000.0
        opp = _opp(sample_market, market_price=0.50, edge=0.20, confidence=1.0, size_usd=3.0)
        size = engine._size_position(opp)
        assert size == pytest.approx(3.0)

    def test_global_cap_binds_in_paper_mode(self, sample_market):
        # Massive edge — Kelly says $100, but paper cap is $10
        engine = TradingEngine()
        engine.balance = 1000.0
        opp = _opp(sample_market, market_price=0.50, edge=0.40, confidence=1.0, size_usd=500.0)
        size = engine._size_position(opp)
        assert size == pytest.approx(settings.simulated_max_position_usd)

    def test_cheap_tail_multiplies_cap(self, sample_market, monkeypatch):
        # Pin the paper cap so we're not at the mercy of the developer's .env.
        monkeypatch.setattr(settings, "simulated_max_position_usd", 10.0)
        monkeypatch.setattr(settings, "cheap_tail_size_multiplier", 2.5)
        engine = TradingEngine()
        engine.positions.clear()
        engine.balance = 1000.0
        # Entry 0.10 — Kelly raw = 1000 * (0.20/0.90) * 0.25 ≈ $55.55,
        # boosted cap = 10 * 2.5 = $25, strategy_cap = $500 → cap binds at $25.
        opp = _opp(sample_market, market_price=0.10, edge=0.20, confidence=1.0, size_usd=500.0)
        assert engine._size_position(opp) == pytest.approx(25.0)

    def test_cheap_tail_does_not_apply_above_threshold(self, sample_market, monkeypatch):
        monkeypatch.setattr(settings, "simulated_max_position_usd", 10.0)
        monkeypatch.setattr(settings, "cheap_tail_size_multiplier", 2.5)
        engine = TradingEngine()
        engine.positions.clear()
        engine.balance = 1000.0
        opp = _opp(sample_market, market_price=settings.cheap_tail_threshold + 0.01,
                   edge=0.20, confidence=1.0, size_usd=500.0)
        # No boost — cap stays at $10.
        assert engine._size_position(opp) == pytest.approx(10.0)

    def test_cheap_tail_disabled_when_multiplier_is_one(self, sample_market, monkeypatch):
        monkeypatch.setattr(settings, "simulated_max_position_usd", 10.0)
        monkeypatch.setattr(settings, "cheap_tail_size_multiplier", 1.0)
        engine = TradingEngine()
        engine.positions.clear()
        engine.balance = 1000.0
        opp = _opp(sample_market, market_price=0.10, edge=0.20, confidence=1.0, size_usd=500.0)
        assert engine._size_position(opp) == pytest.approx(10.0)

    def test_exposure_budget_binds(self, sample_market, open_trade):
        # Exposure cap is 40% of NAV (cash + open exposure), NOT 40% of cash.
        # NAV-based avoids the death-spiral where cap shrinks as you deploy.
        # Setup: balance=$605 cash, $395 already in open positions → NAV=$1000.
        # 40% of NAV = $400 budget; $395 already deployed → $5 remaining.
        engine = TradingEngine()
        engine.positions.clear()   # don't inherit positions from the real trade log on disk
        engine.balance = 605.0
        for i in range(5):
            engine.positions[f"p{i}"] = open_trade.model_copy(update={"opportunity_id": f"p{i}", "size_usd": 79.0})
        opp = _opp(sample_market, market_price=0.50, edge=0.40, confidence=1.0, size_usd=500.0)
        # Kelly raw is large, global cap caps it, but budget=$5 binds tightest.
        size = engine._size_position(opp)
        assert 0.0 < size <= 5.0

    def test_exposure_cap_does_not_collapse_as_balance_drops(self, sample_market, open_trade):
        # Regression: with cash-based 40%, deploying capital shrinks the cap until it
        # collapses to $0 even when only a small fraction of NAV is deployed. NAV-based
        # math fixes this — at 32% NAV deployed there must still be headroom.
        engine = TradingEngine()
        engine.positions.clear()
        # Simulate: started $1000, deployed $316 → cash=$684, NAV=$1000 (no pnl yet).
        # Old buggy math: 40% * $684 - $316 = -$42 → $0. Fixed: 40% * $1000 - $316 = $84.
        engine.balance = 684.0
        for i in range(4):
            engine.positions[f"p{i}"] = open_trade.model_copy(update={"opportunity_id": f"p{i}", "size_usd": 79.0})
        opp = _opp(sample_market, market_price=0.70, edge=0.25, confidence=0.7, size_usd=0.0)
        size = engine._size_position(opp)
        assert size > 0.0, "Exposure cap collapsed — death spiral is back"
