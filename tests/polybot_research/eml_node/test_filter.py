"""
Smoke tests for the Phase 0 audit filter.

Each test corresponds to one of the verified Gamma semantics from the spike
work (vault: projects/eml-neural-ode-polymarket.md Phase 0.1, 0.1.5).
"""

from __future__ import annotations

import json

import pytest

from polybot_research.eml_node.data.filter import keep_resolved_market
from polybot_research.eml_node.data.models import ResolvedMarket


def _build_market(**overrides) -> ResolvedMarket:
    """
    Construct a ResolvedMarket that PASSES the filter, then apply overrides
    to test individual rejection paths in isolation.
    """
    defaults = {
        "id": "1",
        "slug": "test-market",
        "question": "Will X happen?",
        "conditionId": "0xabc",
        "clobTokenIds": json.dumps(["111", "222"]),
        "outcomePrices": json.dumps(["0", "1"]),  # NO won
        "outcomes": json.dumps(["Yes", "No"]),
        "closed": True,
        "fpmmLive": None,  # CLOB-only
        "enableOrderBook": True,
        "marketType": "normal",
        "negRisk": False,
        "negRiskOther": False,
        "volume1yrClob": 50_000.0,
        "volume": 50_000.0,
    }
    defaults.update(overrides)
    return ResolvedMarket.model_validate(defaults)


# ── Acceptance ────────────────────────────────────────────────────────────────


def test_baseline_market_is_kept() -> None:
    """The default market (clean CLOB binary, resolved NO) passes."""
    assert keep_resolved_market(_build_market()) is True


def test_yes_won_is_kept() -> None:
    market = _build_market(outcomePrices=json.dumps(["1", "0"]))
    assert keep_resolved_market(market) is True
    assert market.yes_won is True


# ── AMM/CLOB era guard ────────────────────────────────────────────────────────


def test_amm_era_market_is_dropped() -> None:
    """fpmm_live == True means AMM-era; subgraph has zero data for these."""
    market = _build_market(fpmmLive=True)
    assert keep_resolved_market(market) is False


def test_fpmm_live_none_is_treated_as_clob() -> None:
    """The predicate must be `== True` (not `== False`) — None is the new-CLOB default."""
    market = _build_market(fpmmLive=None)
    assert keep_resolved_market(market) is True


def test_fpmm_live_false_is_treated_as_clob() -> None:
    market = _build_market(fpmmLive=False)
    assert keep_resolved_market(market) is True


# ── Binary-only guard ─────────────────────────────────────────────────────────


def test_non_normal_market_type_is_dropped() -> None:
    market = _build_market(marketType="scalar")
    assert keep_resolved_market(market) is False


def test_neg_risk_bundle_is_dropped() -> None:
    market = _build_market(negRisk=True)
    assert keep_resolved_market(market) is False


# ── Liquidity filter ──────────────────────────────────────────────────────────


def test_zero_clob_volume_is_dropped() -> None:
    market = _build_market(
        volume1yrClob=0.0, volume1moClob=0.0, volume1wkClob=0.0
    )
    assert keep_resolved_market(market) is False


def test_recent_market_with_null_yearly_volume_kept_via_monthly() -> None:
    """volume_1yr_clob is null on very recent markets — coalesce must save them."""
    market = _build_market(volume1yrClob=None, volume1moClob=42_000.0)
    assert keep_resolved_market(market) is True


def test_below_volume_threshold_is_dropped() -> None:
    market = _build_market(volume1yrClob=10.0)
    assert (
        keep_resolved_market(market, min_clob_volume_usd=1_000.0) is False
    )


def test_enable_orderbook_false_is_dropped() -> None:
    market = _build_market(enableOrderBook=False)
    assert keep_resolved_market(market) is False


# ── Resolution-payout guard (Phase 0.1.5 ground truth) ────────────────────────


def test_pre_uma_zero_zero_outcome_is_dropped() -> None:
    """The 2020-Biden-COVID artifact: outcomePrices = ['0', '0']."""
    market = _build_market(outcomePrices=json.dumps(["0", "0"]))
    assert market.yes_won is None
    assert keep_resolved_market(market) is False


def test_unparseable_outcome_prices_dropped() -> None:
    market = _build_market(outcomePrices="not json")
    assert keep_resolved_market(market) is False


def test_three_outcome_market_dropped() -> None:
    market = _build_market(outcomePrices=json.dumps(["0", "0.5", "0.5"]))
    assert keep_resolved_market(market) is False


# ── Token ID guard ────────────────────────────────────────────────────────────


def test_missing_token_id_dropped() -> None:
    market = _build_market(clobTokenIds=json.dumps(["111", ""]))
    assert keep_resolved_market(market) is False


def test_unparseable_token_ids_dropped() -> None:
    market = _build_market(clobTokenIds="garbage")
    assert keep_resolved_market(market) is False


# ── Closed-state guard ────────────────────────────────────────────────────────


def test_open_market_dropped() -> None:
    market = _build_market(closed=False)
    assert keep_resolved_market(market) is False


# ── Model parsing sanity ──────────────────────────────────────────────────────


def test_yes_won_returns_true_for_one_zero() -> None:
    market = _build_market(outcomePrices=json.dumps(["1", "0"]))
    assert market.yes_won is True


def test_yes_won_returns_false_for_zero_one() -> None:
    market = _build_market(outcomePrices=json.dumps(["0", "1"]))
    assert market.yes_won is False


def test_clob_token_ids_round_trip() -> None:
    market = _build_market(clobTokenIds=json.dumps(["abc", "def"]))
    assert market.clob_token_ids == ["abc", "def"]


# ── Imports / structure smoke ─────────────────────────────────────────────────


def test_package_imports_cleanly() -> None:
    """Importing the package shouldn't pull torch (research extras only)."""
    import polybot_research
    import polybot_research.eml_node
    import polybot_research.eml_node.data
    import polybot_research.eml_node.data.filter as filter_mod
    import polybot_research.eml_node.data.gamma as gamma_mod
    import polybot_research.eml_node.data.models as models_mod
    import polybot_research.eml_node.data.subgraph as subgraph_mod

    # A bare import should not fail even if torch isn't installed.
    import polybot_research.eml_node.eml as eml_mod
    import polybot_research.eml_node.node as node_mod

    assert hasattr(filter_mod, "keep_resolved_market")
    assert hasattr(gamma_mod, "ResolvedMarketsClient")
    assert hasattr(models_mod, "ResolvedMarket")
    assert hasattr(subgraph_mod, "SubgraphClient")
    assert hasattr(eml_mod, "__all__")
    assert hasattr(node_mod, "__all__")


def test_subgraph_client_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing without a key should fail fast, not at first request."""
    from polybot_research.eml_node.data.subgraph import SubgraphClient

    monkeypatch.delenv("GRAPH_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GRAPH_API_KEY"):
        SubgraphClient()


def test_subgraph_url_has_no_trailing_slash() -> None:
    """
    Regression: the Graph gateway 404s on a trailing slash. We deliberately
    don't use httpx base_url because empty-path POSTs append "/". This test
    asserts the absolute URL constant is well-formed and the client doesn't
    try to be clever.
    """
    from polybot_research.eml_node.data.subgraph import SUBGRAPH_URL

    assert SUBGRAPH_URL.startswith("https://gateway.thegraph.com/api/subgraphs/id/")
    assert not SUBGRAPH_URL.endswith("/"), (
        "SUBGRAPH_URL must not end with '/' — Graph gateway returns 404 on "
        "trailing slash. See subgraph.py::__init__ comment."
    )
