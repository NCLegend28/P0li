"""
Implied-probability derivation from raw OrderFilledEvent records.

Verified empirically 2026-05-10 against the first end-to-end Phase 0 pull
(8 Espresso-FDV markets, 16 assets, ~10K fills) by comparing per-fill derived
probabilities against known resolution outcomes from Gamma's `outcomePrices`.

The rule
--------
Mechanically, the subgraph's ``price`` field equals
``takerAmountFilled / makerAmountFilled`` (verified row-by-row, no
discrepancies).

The semantic meaning of that ratio depends on ``side``:

    side == "sell":  price is USDC-per-token       → probability = price
    side == "buy":   price is tokens-per-USDC      → probability = 1 / price

Asset identity (YES vs NO) determines which probability you've recovered.
For binary markets ``YES_prob + NO_prob == 1``, so the NO-token's implied
probability inverts to YES via ``yes_prob = 1 - no_prob``.

References
----------
- Gamma `outcomePrices` for resolved markets is `["0","1"]` or `["1","0"]`
  (Phase 0.1.5 verification).
- Subgraph `OrderFilledEvent` schema introspected 2026-05-10
  (data/research/eml_node/schema_snapshots/2026-05-10.json).
"""

from __future__ import annotations

from polybot_research.eml_node.data.models import Fill

# Side string values observed empirically. Subgraph returns lowercase.
SIDE_BUY = "buy"
SIDE_SELL = "sell"


def implied_probability_of_token(fill: Fill) -> float:
    """
    Return the implied probability of the *fill's own token* at trade time.

    If the fill is on the YES asset, this is the implied YES probability.
    If on the NO asset, this is the implied NO probability.

    Always in (0, 1] for well-formed fills. Returns ``float("nan")`` if the
    rule cannot be applied (unrecognized side, zero price).
    """
    if fill.price <= 0:
        return float("nan")
    side = fill.side.lower()
    if side == SIDE_SELL:
        return fill.price
    if side == SIDE_BUY:
        return 1.0 / fill.price
    return float("nan")


def implied_yes_probability(fill: Fill, *, is_yes_token: bool) -> float:
    """
    Return the implied YES probability for a fill.

    Parameters
    ----------
    fill : a Fill record from polybot_research.eml_node.data.models
    is_yes_token : True if the queried asset_id matches the market's
        clob_token_id_yes (i.e. this fill traded the YES token); False if
        it matches clob_token_id_no.

    For YES-token fills: yes_prob = implied_probability_of_token(fill).
    For NO-token fills:  yes_prob = 1 - implied_probability_of_token(fill).
    """
    p = implied_probability_of_token(fill)
    if p != p:  # NaN check
        return float("nan")
    return p if is_yes_token else 1.0 - p
