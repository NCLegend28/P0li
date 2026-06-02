"""
Audit-ready client-side filter for resolved Polymarket markets.

Codifies the verified Gamma semantics from the vault project page Phase 0.1
and 0.1.5 spikes. Every predicate is grounded in a real observation:

1. AMM-era markets (`fpmm_live == True`) traded on the Fixed Product Market
   Maker before ~2022 — they have ZERO records in the CLOB subgraph. Note:
   `fpmm_live` is `None` (not `False`) on new CLOB-only markets, so the
   predicate must be "skip if `== True`", NOT "keep if `== False`".

2. Multi-outcome bundles (`neg_risk == True` or `market_type != "normal"`)
   are out of scope for this binary-EML-NODE project.

3. CLOB activity check uses a coalesce of three volume fields. `volume_1yr_clob`
   is `None` on markets closed within ~60 days — using it alone would silently
   drop fresh data.

4. `outcome_prices` must encode a clean binary resolution as `["0","1"]` or
   `["1","0"]` — any other shape (unresolved, disputed, pre-UMA stale `["0","0"]`)
   is dropped. This is verified ground truth on UMA-era markets per Phase 0.1.5.

5. `clob_token_ids` must parse to exactly two non-empty strings (binary market
   with both tokens minted).

Gamma silently ignores unknown filter parameters (verified by passing
`fpmm_live=false` and getting back `fpmmLive: true`), so all of this filtering
must happen client-side.
"""

from __future__ import annotations

from polybot_research.eml_node.data.models import ResolvedMarket


def keep_resolved_market(
    m: ResolvedMarket,
    *,
    min_clob_volume_usd: float = 1_000.0,
) -> bool:
    """
    Return True if the market should be kept for the EML-NODE dataset.

    The threshold defaults to $1,000 of CLOB volume — generous enough to
    include thinner markets while excluding markets that never traded.
    Tighten in Phase 0.6 once we see the empirical distribution.
    """
    # 1. Must be closed
    if not m.closed:
        return False

    # 2. AMM-era guard. Must be == True; None means new CLOB-only.
    if m.fpmm_live is True:
        return False

    # 3. Binary only. Drop multi-outcome bundles.
    if m.market_type != "normal":
        return False
    if m.neg_risk or m.neg_risk_other:
        return False

    # 4. CLOB activity. Coalesce yearly → monthly → weekly to avoid punishing
    # fresh markets where the yearly stat is null.
    clob_vol = (
        m.volume_1yr_clob
        if m.volume_1yr_clob is not None
        else m.volume_1mo_clob
        if m.volume_1mo_clob is not None
        else m.volume_1wk_clob
        if m.volume_1wk_clob is not None
        else 0.0
    )
    if not (m.enable_order_book is True and clob_vol > min_clob_volume_usd):
        return False

    # 5. Resolved with clean binary payout.
    if m.yes_won is None:
        return False

    # 6. Both CLOB token IDs present (binary market with both outcomes minted).
    if len(m.clob_token_ids) != 2:
        return False

    return True
