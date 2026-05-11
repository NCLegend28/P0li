"""
Tests for Phase 0.5 bucketing.

Covers:
- Single-bucket aggregation (mean / VWAP / first / last / count)
- Multi-bucket separation by hour boundary
- Joining YES + NO asset fills into one YES-probability series
- Forward-fill into empty buckets vs sparse output
- USDC accounting per side
- Edge cases: empty input, all-bad fills
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import polars as pl
import pytest

from polybot_research.eml_node.data.bucketing import (
    DEFAULT_BUCKET_SECONDS,
    _usdc_amount,
    bucket_market_fills,
)
from polybot_research.eml_node.data.models import Fill, ResolvedMarket


# ── Fixture builders ──────────────────────────────────────────────────────────


def _make_market(closed_time: str | None = None) -> ResolvedMarket:
    return ResolvedMarket.model_validate(
        {
            "id": "test",
            "slug": "test-market",
            "question": "Test?",
            "conditionId": "0xabc123",
            "clobTokenIds": json.dumps(["yes_id", "no_id"]),
            "outcomePrices": json.dumps(["1", "0"]),
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


def _make_fill(
    *,
    timestamp: int,
    side: str,
    price: float,
    maker_amount: int = 1_000_000,  # default 1 unit
    taker_amount: int = 1_000_000,
    block_number: int = 0,
) -> Fill:
    return Fill(
        id=f"fill-{timestamp}-{side}",
        transaction_hash="0xtx",
        timestamp=timestamp,
        block_number=block_number,
        order_hash="0xo",
        maker="0xm",
        taker="0xt",
        maker_asset_id="0",
        taker_asset_id="0",
        maker_amount_filled=maker_amount,
        taker_amount_filled=taker_amount,
        price=price,
        side=side,
        fee=0,
    )


# ── USDC accounting ───────────────────────────────────────────────────────────


def test_usdc_amount_sell_side_uses_taker() -> None:
    """On a sell, taker provides USDC."""
    f = _make_fill(timestamp=0, side="sell", price=0.5,
                   taker_amount=2_500_000, maker_amount=5_000_000)
    assert _usdc_amount(f) == pytest.approx(2.5)


def test_usdc_amount_buy_side_uses_maker() -> None:
    """On a buy, maker provides USDC."""
    f = _make_fill(timestamp=0, side="buy", price=2.0,
                   maker_amount=2_500_000, taker_amount=5_000_000)
    assert _usdc_amount(f) == pytest.approx(2.5)


def test_usdc_amount_unknown_side_zero() -> None:
    f = _make_fill(timestamp=0, side="LIMIT", price=0.5)
    assert _usdc_amount(f) == 0.0


# ── Single-bucket aggregation ─────────────────────────────────────────────────


def test_single_bucket_mean_and_vwap() -> None:
    """Three YES-side sells in the same hour → one bucket."""
    market = _make_market()
    fills = [
        _make_fill(timestamp=1000, side="sell", price=0.4, taker_amount=1_000_000, block_number=1),
        _make_fill(timestamp=2000, side="sell", price=0.6, taker_amount=3_000_000, block_number=2),
        _make_fill(timestamp=3000, side="sell", price=0.5, taker_amount=2_000_000, block_number=3),
    ]
    buckets = bucket_market_fills(
        market=market, yes_fills=fills, no_fills=[], forward_fill=False
    )
    assert buckets.height == 1
    row = buckets.row(0, named=True)
    # Mean: (0.4 + 0.6 + 0.5) / 3 = 0.5
    assert row["yes_prob_mean"] == pytest.approx(0.5)
    # VWAP: (0.4·1 + 0.6·3 + 0.5·2) / 6 = (0.4 + 1.8 + 1.0) / 6 = 0.5333…
    assert row["yes_prob_vwap"] == pytest.approx((0.4 + 1.8 + 1.0) / 6.0)
    assert row["yes_prob_first"] == pytest.approx(0.4)
    assert row["yes_prob_last"] == pytest.approx(0.5)
    assert row["n_fills"] == 3
    assert row["n_yes_fills"] == 3
    assert row["n_no_fills"] == 0
    assert row["usdc_volume"] == pytest.approx(6.0)
    assert row["is_synthetic"] is False


# ── Multi-bucket separation ───────────────────────────────────────────────────


def test_fills_separate_into_hour_buckets() -> None:
    """Fills 2 hours apart land in different buckets."""
    market = _make_market()
    fills = [
        _make_fill(timestamp=10, side="sell", price=0.3),
        _make_fill(timestamp=10 + DEFAULT_BUCKET_SECONDS, side="sell", price=0.7),
    ]
    buckets = bucket_market_fills(
        market=market, yes_fills=fills, no_fills=[], forward_fill=False
    )
    assert buckets.height == 2
    starts = sorted(buckets["bucket_start_ts"].to_list())
    assert starts == [0, DEFAULT_BUCKET_SECONDS]


# ── YES + NO joining ──────────────────────────────────────────────────────────


def test_yes_and_no_fills_combine_into_yes_probability() -> None:
    """
    A YES-token sell @ 0.7 and a NO-token sell @ 0.3 in the same bucket should
    both yield YES_prob = 0.7 (NO inverts to YES = 1 - 0.3).
    """
    market = _make_market()
    yes_fills = [_make_fill(timestamp=100, side="sell", price=0.7,
                            taker_amount=1_000_000)]
    no_fills = [_make_fill(timestamp=200, side="sell", price=0.3,
                           taker_amount=1_000_000)]
    buckets = bucket_market_fills(
        market=market, yes_fills=yes_fills, no_fills=no_fills, forward_fill=False
    )
    assert buckets.height == 1
    row = buckets.row(0, named=True)
    # Both fills give YES_prob = 0.7 → mean = 0.7
    assert row["yes_prob_mean"] == pytest.approx(0.7)
    assert row["n_yes_fills"] == 1
    assert row["n_no_fills"] == 1
    assert row["n_fills"] == 2


# ── Forward-fill ──────────────────────────────────────────────────────────────


def test_forward_fill_creates_uniform_grid() -> None:
    """Two real fills 3 hours apart → 4 rows on a forward-filled grid."""
    market = _make_market()
    fills = [
        _make_fill(timestamp=0, side="sell", price=0.4),
        _make_fill(timestamp=3 * DEFAULT_BUCKET_SECONDS, side="sell", price=0.7),
    ]
    buckets = bucket_market_fills(
        market=market, yes_fills=fills, no_fills=[], forward_fill=True
    )
    # Buckets at ts = 0, 3600, 7200, 10800
    assert buckets.height == 4
    rows = buckets.sort("bucket_start_ts").to_dicts()
    # Bucket 0: real, prob=0.4
    assert rows[0]["is_synthetic"] is False
    assert rows[0]["yes_prob_mean"] == pytest.approx(0.4)
    # Buckets 1 and 2: synthetic, forward-filled to 0.4
    assert rows[1]["is_synthetic"] is True
    assert rows[1]["yes_prob_mean"] == pytest.approx(0.4)
    assert rows[2]["is_synthetic"] is True
    assert rows[2]["yes_prob_mean"] == pytest.approx(0.4)
    # Bucket 3: real, prob=0.7
    assert rows[3]["is_synthetic"] is False
    assert rows[3]["yes_prob_mean"] == pytest.approx(0.7)
    # Synthetic rows have zero fills and zero USDC
    assert rows[1]["n_fills"] == 0
    assert rows[1]["usdc_volume"] == 0.0


def test_forward_fill_extends_to_closed_time() -> None:
    """Grid extends to bucket containing closed_time even if no fills there."""
    closed_dt = datetime.fromtimestamp(5 * DEFAULT_BUCKET_SECONDS + 30, tz=timezone.utc)
    closed_str = closed_dt.strftime("%Y-%m-%d %H:%M:%S+00")
    market = _make_market(closed_time=closed_str)
    fills = [_make_fill(timestamp=0, side="sell", price=0.5)]
    buckets = bucket_market_fills(
        market=market, yes_fills=fills, no_fills=[], forward_fill=True
    )
    # Last bucket should be the one containing closed_time = 5 * BUCKET
    assert int(buckets["bucket_start_ts"].max()) == 5 * DEFAULT_BUCKET_SECONDS
    # All synthetic rows forward-filled to 0.5
    assert all(buckets["yes_prob_mean"].to_list()) is not None


# ── Sparse mode ───────────────────────────────────────────────────────────────


def test_no_forward_fill_keeps_only_real_buckets() -> None:
    market = _make_market()
    fills = [
        _make_fill(timestamp=0, side="sell", price=0.4),
        _make_fill(timestamp=3 * DEFAULT_BUCKET_SECONDS, side="sell", price=0.7),
    ]
    buckets = bucket_market_fills(
        market=market, yes_fills=fills, no_fills=[], forward_fill=False
    )
    assert buckets.height == 2
    assert not buckets["is_synthetic"].any()


# ── Defensive cases ───────────────────────────────────────────────────────────


def test_empty_inputs_return_empty_frame() -> None:
    market = _make_market()
    buckets = bucket_market_fills(
        market=market, yes_fills=[], no_fills=[], forward_fill=True
    )
    assert buckets.is_empty()
    # Schema is still well-formed
    assert "yes_prob_mean" in buckets.columns


def test_all_bad_fills_dropped() -> None:
    """Fills with zero price → NaN prob → dropped → empty frame."""
    market = _make_market()
    fills = [_make_fill(timestamp=0, side="sell", price=0.0)]
    buckets = bucket_market_fills(
        market=market, yes_fills=fills, no_fills=[], forward_fill=True
    )
    assert buckets.is_empty()


def test_out_of_range_yes_prob_dropped() -> None:
    """YES-token sell @ 1.5 would yield prob 1.5 → out of range → dropped."""
    market = _make_market()
    fills = [_make_fill(timestamp=0, side="sell", price=1.5)]
    buckets = bucket_market_fills(
        market=market, yes_fills=fills, no_fills=[], forward_fill=False
    )
    assert buckets.is_empty()


# ── Ordering: first / last respect block ordering, not timestamp ──────────────


def test_first_last_use_block_ordering_within_bucket() -> None:
    """
    Two fills at the same timestamp: first/last should be ordered by
    block_number, not by list position.
    """
    market = _make_market()
    fills = [
        _make_fill(timestamp=100, side="sell", price=0.7, block_number=20),  # later block
        _make_fill(timestamp=100, side="sell", price=0.3, block_number=10),  # earlier block
    ]
    buckets = bucket_market_fills(
        market=market, yes_fills=fills, no_fills=[], forward_fill=False
    )
    row = buckets.row(0, named=True)
    assert row["yes_prob_first"] == pytest.approx(0.3)  # block 10
    assert row["yes_prob_last"] == pytest.approx(0.7)  # block 20
