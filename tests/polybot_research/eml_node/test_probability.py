"""
Tests for implied-probability derivation from OrderFilledEvent records.

Each test case is grounded in an empirically observed last-trade record from
the first end-to-end Phase 0 pull (2026-05-10) on the Espresso-FDV markets,
cross-checked against Gamma's `outcomePrices` resolution.

If the subgraph ever changes the meaning of `price` or `side`, these tests
will catch it.
"""

from __future__ import annotations

from polybot_research.eml_node.data.models import Fill
from polybot_research.eml_node.data.probability import (
    implied_probability_of_token,
    implied_yes_probability,
)


def _make_fill(*, price: float, side: str) -> Fill:
    """Minimal Fill for probability checks. Fields irrelevant to the rule are dummy values."""
    return Fill(
        id="dummy",
        transaction_hash="0x0",
        timestamp=0,
        block_number=0,
        order_hash="0x0",
        maker="0x0",
        taker="0x0",
        maker_asset_id="0",
        taker_asset_id="0",
        maker_amount_filled=0,
        taker_amount_filled=0,
        price=price,
        side=side,
        fee=0,
    )


# ── Sell side: price is the implied probability directly ──────────────────────


def test_sell_side_price_is_probability_high() -> None:
    """NO of 'Espresso > $300M' (NO won): last sell @ 0.999 → prob ≈ 0.999."""
    fill = _make_fill(price=0.999, side="sell")
    assert abs(implied_probability_of_token(fill) - 0.999) < 1e-9


def test_sell_side_price_is_probability_low() -> None:
    """NO of 'Espresso > $200M' (YES won): last sell @ 0.001 → prob ≈ 0.001."""
    fill = _make_fill(price=0.001, side="sell")
    assert abs(implied_probability_of_token(fill) - 0.001) < 1e-9


# ── Buy side: probability is the inverse of price ─────────────────────────────


def test_buy_side_inverse_of_price() -> None:
    """YES of 'Espresso > $500M' (YES lost): buy @ 1000.0 → prob = 1/1000 = 0.001."""
    fill = _make_fill(price=1000.0, side="buy")
    assert abs(implied_probability_of_token(fill) - 0.001) < 1e-9


def test_buy_side_near_one() -> None:
    """YES of 'Espresso > $200M' (YES won): buy @ 1.001 → prob ≈ 0.999."""
    fill = _make_fill(price=1.001, side="buy")
    expected = 1.0 / 1.001
    assert abs(implied_probability_of_token(fill) - expected) < 1e-9


# ── YES-token vs NO-token disambiguation ──────────────────────────────────────


def test_yes_token_fill_returns_yes_probability() -> None:
    """For a YES-token sell @ 0.65, YES probability is 0.65 directly."""
    fill = _make_fill(price=0.65, side="sell")
    assert abs(implied_yes_probability(fill, is_yes_token=True) - 0.65) < 1e-9


def test_no_token_fill_inverts_to_yes() -> None:
    """For a NO-token sell @ 0.35, YES probability is 1 - 0.35 = 0.65."""
    fill = _make_fill(price=0.35, side="sell")
    assert abs(implied_yes_probability(fill, is_yes_token=False) - 0.65) < 1e-9


def test_no_token_buy_inverts_to_yes() -> None:
    """NO-token buy @ 2.0 means NO probability = 0.5, so YES probability = 0.5."""
    fill = _make_fill(price=2.0, side="buy")
    assert abs(implied_yes_probability(fill, is_yes_token=False) - 0.5) < 1e-9


# ── Defensive cases ───────────────────────────────────────────────────────────


def test_zero_price_returns_nan() -> None:
    fill = _make_fill(price=0.0, side="sell")
    p = implied_probability_of_token(fill)
    assert p != p  # NaN


def test_negative_price_returns_nan() -> None:
    fill = _make_fill(price=-0.1, side="sell")
    p = implied_probability_of_token(fill)
    assert p != p  # NaN


def test_unknown_side_returns_nan() -> None:
    fill = _make_fill(price=0.5, side="LIMIT")
    p = implied_probability_of_token(fill)
    assert p != p  # NaN


def test_uppercase_side_handled() -> None:
    """The subgraph returns lowercase, but be tolerant."""
    fill = _make_fill(price=0.7, side="SELL")
    assert abs(implied_probability_of_token(fill) - 0.7) < 1e-9
