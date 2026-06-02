"""
Tests for Phase 0.6 sanity cross-check.

Each test exercises one specific failure mode of the bucketed-vs-resolution
comparison so we know exactly what the check is testing for.
"""

from __future__ import annotations

import json

import polars as pl
import pytest

from polybot_research.eml_node.data.bucketing import bucket_market_fills
from polybot_research.eml_node.data.models import Fill, ResolvedMarket
from polybot_research.eml_node.data.sanity import (
    DEFAULT_THRESHOLD,
    check_market,
    summarize,
)


# ── Fixture builders ──────────────────────────────────────────────────────────


def _make_market(*, yes_won: bool, closed_time: str | None = None) -> ResolvedMarket:
    return ResolvedMarket.model_validate(
        {
            "id": "test",
            "slug": "test-market",
            "question": "Test?",
            "conditionId": "0xabc123",
            "clobTokenIds": json.dumps(["yes_id", "no_id"]),
            "outcomePrices": json.dumps(["1", "0"] if yes_won else ["0", "1"]),
            "outcomes": json.dumps(["Yes", "No"]),
            "closed": True,
            "closedTime": closed_time,
            "fpmmLive": None,
            "enableOrderBook": True,
            "marketType": "normal",
            "negRisk": False,
            "negRiskOther": False,
            "volume1yrClob": 50_000.0,
            "volume": 50_000.0,
        }
    )


def _make_fill(*, timestamp: int, side: str, price: float, block_number: int = 0) -> Fill:
    return Fill(
        id=f"f-{timestamp}-{block_number}",
        transaction_hash=f"0x{timestamp:x}",
        timestamp=timestamp,
        block_number=block_number,
        order_hash="0x0",
        maker="0x0",
        taker="0x0",
        maker_asset_id="0",
        taker_asset_id="0",
        maker_amount_filled=1_000_000,
        taker_amount_filled=1_000_000,
        price=price,
        side=side,
        fee=0,
    )


def _bucket(market: ResolvedMarket, fills: list[Fill]) -> "pl.DataFrame":
    return bucket_market_fills(
        market=market, yes_fills=fills, no_fills=[], forward_fill=False
    )


# ── Pass cases ────────────────────────────────────────────────────────────────


def test_yes_market_with_final_prob_999_passes() -> None:
    """YES won + final YES_prob 0.999 → discrepancy 0.001 < threshold."""
    market = _make_market(yes_won=True)
    fills = [
        _make_fill(timestamp=0, side="sell", price=0.5),
        _make_fill(timestamp=7200, side="sell", price=0.999, block_number=2),
    ]
    df = _bucket(market, fills)
    result = check_market(market, df)
    assert result.passed
    assert result.expected_final_yes_prob == 1.0
    assert result.measured_final_yes_prob == pytest.approx(0.999)
    assert result.discrepancy == pytest.approx(0.001)


def test_no_market_with_final_prob_001_passes() -> None:
    market = _make_market(yes_won=False)
    fills = [
        _make_fill(timestamp=0, side="sell", price=0.5),
        _make_fill(timestamp=7200, side="sell", price=0.001, block_number=2),
    ]
    df = _bucket(market, fills)
    result = check_market(market, df)
    assert result.passed
    assert result.expected_final_yes_prob == 0.0
    assert result.measured_final_yes_prob == pytest.approx(0.001)


def test_threshold_boundary_passes_just_under_threshold() -> None:
    """Discrepancy just under the threshold passes; just over fails."""
    market = _make_market(yes_won=True)
    # 0.96 → discrepancy 0.04 < 0.05 threshold
    fills = [_make_fill(timestamp=0, side="sell", price=0.96)]
    df = _bucket(market, fills)
    result = check_market(market, df)
    assert result.passed
    assert result.discrepancy == pytest.approx(0.04)


# ── Fail cases (the load-bearing ones) ────────────────────────────────────────


def test_yes_market_with_final_prob_close_to_zero_fails() -> None:
    """If YES won but final YES prob is 0.05, something is broken."""
    market = _make_market(yes_won=True)
    fills = [_make_fill(timestamp=0, side="sell", price=0.05)]
    df = _bucket(market, fills)
    result = check_market(market, df)
    assert not result.passed
    assert result.discrepancy == pytest.approx(0.95)
    assert "discrepancy" in result.note


def test_no_market_with_final_prob_close_to_one_fails() -> None:
    """If NO won but final YES prob is 0.95, the join keys are likely swapped."""
    market = _make_market(yes_won=False)
    fills = [_make_fill(timestamp=0, side="sell", price=0.95)]
    df = _bucket(market, fills)
    result = check_market(market, df)
    assert not result.passed
    assert result.discrepancy == pytest.approx(0.95)


def test_threshold_just_exceeded_fails() -> None:
    market = _make_market(yes_won=True)
    fills = [_make_fill(timestamp=0, side="sell", price=1.0 - DEFAULT_THRESHOLD - 1e-6)]
    df = _bucket(market, fills)
    result = check_market(market, df)
    assert not result.passed


# ── Edge cases ────────────────────────────────────────────────────────────────


def test_empty_buckets_fails_with_diagnostic_note() -> None:
    market = _make_market(yes_won=True)
    df = _bucket(market, [])
    result = check_market(market, df)
    assert not result.passed
    assert "empty" in result.note.lower()
    assert result.measured_final_yes_prob != result.measured_final_yes_prob  # NaN


def test_unparseable_yes_won_fails_with_diagnostic_note() -> None:
    """If outcome_prices was [0,0] (pre-UMA junk), yes_won is None."""
    market = ResolvedMarket.model_validate(
        {
            "id": "broken",
            "slug": "broken",
            "question": "?",
            "conditionId": "0x0",
            "clobTokenIds": json.dumps(["a", "b"]),
            "outcomePrices": json.dumps(["0", "0"]),  # unresolved
            "outcomes": json.dumps(["Yes", "No"]),
            "closed": True,
            "marketType": "normal",
            "fpmmLive": None,
            "enableOrderBook": True,
            "volume": 50_000.0,
            "volume1yrClob": 50_000.0,
        }
    )
    fills = [_make_fill(timestamp=0, side="sell", price=0.5)]
    df = _bucket(_make_market(yes_won=True), fills)
    result = check_market(market, df)
    assert not result.passed
    assert "yes_won" in result.note


def test_only_synthetic_buckets_fails() -> None:
    """If forward-fill produced only synthetic rows, there are no real trades."""
    import polars as pl

    market = _make_market(yes_won=True)
    df = pl.DataFrame(
        {
            "bucket_start_ts": [0, 3600, 7200],
            "yes_prob_mean": [0.5, 0.5, 0.5],
            "yes_prob_vwap": [0.5, 0.5, 0.5],
            "yes_prob_first": [0.5, 0.5, 0.5],
            "yes_prob_last": [0.5, 0.5, 0.5],
            "n_fills": [0, 0, 0],
            "n_yes_fills": [0, 0, 0],
            "n_no_fills": [0, 0, 0],
            "usdc_volume": [0.0, 0.0, 0.0],
            "is_synthetic": [True, True, True],
        }
    )
    result = check_market(market, df)
    assert not result.passed
    assert "synthetic" in result.note.lower()


def test_threshold_is_configurable() -> None:
    """A stricter threshold should turn a previously-passing market into a fail."""
    market = _make_market(yes_won=True)
    fills = [_make_fill(timestamp=0, side="sell", price=0.96)]
    df = _bucket(market, fills)

    loose = check_market(market, df, threshold=0.05)
    strict = check_market(market, df, threshold=0.01)

    assert loose.passed
    assert not strict.passed


# ── Reporting ─────────────────────────────────────────────────────────────────


def test_summarize_returns_pass_rate() -> None:
    """summarize over a mix should report the right counts."""
    market_yes = _make_market(yes_won=True)
    market_no = _make_market(yes_won=False)
    df_pass = _bucket(market_yes, [_make_fill(timestamp=0, side="sell", price=0.99)])
    df_fail = _bucket(market_yes, [_make_fill(timestamp=0, side="sell", price=0.10)])

    results = [
        check_market(market_yes, df_pass),
        check_market(market_yes, df_fail),
        check_market(market_no, _bucket(market_no, [_make_fill(timestamp=0, side="sell", price=0.01)])),
    ]
    metrics = summarize(results)
    assert metrics["n_markets"] == 3
    assert metrics["n_passed"] == 2
    assert metrics["n_failed"] == 1
    assert metrics["pass_rate"] == pytest.approx(2 / 3)


def test_summarize_handles_empty() -> None:
    metrics = summarize([])
    assert metrics["n_markets"] == 0
    assert metrics["pass_rate"] == 0.0
