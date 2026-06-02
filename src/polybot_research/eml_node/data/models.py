"""
Data models for the Phase 0 pipeline.

Why these are separate from polybot.models.Market:
    polybot.models.Market is shaped for the *live* paper-trading bot — it has
    yes_price/no_price/hours_until_close as computed properties on a
    currently-open market. We need a richer schema for resolved markets:
    resolution payouts, condition_id, clob_token_ids, fpmm/CLOB era discriminators,
    and the volume metrics that feed the AMM/CLOB filter.

These models are pydantic for parse-time validation. Once we hit the bucketing
stage we move to polars/pyarrow for performance.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ResolvedMarket(BaseModel):
    """
    A binary-outcome Polymarket market that has resolved.

    All fields here are required for downstream Phase 0 steps. Anything that
    Gamma might omit on some markets is marked Optional and the filter in
    polybot_research.eml_node.data.filter handles the missing-data cases.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    # ── Identity ──────────────────────────────────────────────────────────────
    id: str
    slug: str = ""
    question: str

    # ── Join keys (critical) ──────────────────────────────────────────────────
    # CTF condition hash; matches subgraph MarketData.condition
    condition_id: str = Field(alias="conditionId")
    # JSON-encoded "[token_a_id, token_b_id]"; matches subgraph
    # OrderFilledEvent.makerAssetId / takerAssetId
    clob_token_ids_raw: str = Field(alias="clobTokenIds")

    # ── Resolution ground truth ───────────────────────────────────────────────
    # JSON-encoded "[\"0\",\"1\"]" or "[\"1\",\"0\"]" for resolved binary markets;
    # Gamma also returns "[\"0\",\"0\"]" for stale pre-UMA markets (filter drops these).
    outcome_prices_raw: str = Field(alias="outcomePrices")
    outcomes_raw: str = Field(alias="outcomes", default='["Yes","No"]')

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    closed: bool = False
    closed_time: str | None = Field(alias="closedTime", default=None)
    end_date: datetime | None = Field(alias="endDate", default=None)
    end_date_iso: str | None = Field(alias="endDateIso", default=None)

    # ── AMM/CLOB era discriminators (filter signals) ──────────────────────────
    # AMM-era markets have fpmm_live=True; CLOB-only have fpmm_live=None or False.
    # Subgraph indexes CLOB only — fpmm_live=True markets must be filtered out.
    fpmm_live: bool | None = Field(alias="fpmmLive", default=None)
    enable_order_book: bool | None = Field(alias="enableOrderBook", default=None)

    # ── Liquidity / volume (for filtering and stratification) ─────────────────
    volume: float = 0.0
    volume_1yr_clob: float | None = Field(alias="volume1yrClob", default=None)
    volume_1mo_clob: float | None = Field(alias="volume1moClob", default=None)
    volume_1wk_clob: float | None = Field(alias="volume1wkClob", default=None)
    liquidity_clob: float | None = Field(alias="liquidityClob", default=None)
    liquidity_amm: float | None = Field(alias="liquidityAmm", default=None)

    # ── Market type (we only want binary "normal", not multi-outcome) ─────────
    market_type: str = Field(alias="marketType", default="normal")
    neg_risk: bool = Field(alias="negRisk", default=False)
    neg_risk_other: bool = Field(alias="negRiskOther", default=False)

    # ── UMA resolution metadata ───────────────────────────────────────────────
    uma_resolution_statuses: str | None = Field(
        alias="umaResolutionStatuses", default=None
    )

    # ── Derived helpers ───────────────────────────────────────────────────────

    @field_validator("volume", mode="before")
    @classmethod
    def _coerce_volume(cls, v: Any) -> float:
        if v is None or v == "":
            return 0.0
        return float(v)

    @property
    def clob_token_ids(self) -> list[str]:
        """Parse the JSON-encoded clob_token_ids field."""
        import json

        try:
            ids = json.loads(self.clob_token_ids_raw)
            return [str(x) for x in ids if x]
        except (json.JSONDecodeError, TypeError):
            return []

    @property
    def outcome_prices(self) -> list[float]:
        """Parse the JSON-encoded outcome_prices field."""
        import json

        try:
            return [float(x) for x in json.loads(self.outcome_prices_raw)]
        except (json.JSONDecodeError, TypeError, ValueError):
            return []

    @property
    def outcomes(self) -> list[str]:
        """Parse the JSON-encoded outcomes field."""
        import json

        try:
            return [str(x) for x in json.loads(self.outcomes_raw)]
        except (json.JSONDecodeError, TypeError):
            return []

    @property
    def yes_won(self) -> bool | None:
        """
        Returns True if YES resolved (outcome_prices == [1, 0]),
        False if NO resolved ([0, 1]),
        None if unresolved or ambiguous.

        Per verified Gamma semantics (see vault project page Phase 0.1.5):
        for resolved binary markets, outcome_prices is exactly ["0","1"] or ["1","0"].
        Anything else means the market hasn't reliably resolved.
        """
        prices = self.outcome_prices
        if len(prices) != 2:
            return None
        if prices == [1.0, 0.0]:
            return True
        if prices == [0.0, 1.0]:
            return False
        return None


class Fill(BaseModel):
    """
    A single OrderFilledEvent record from the CTF Exchange subgraph.

    Notes on `price` (verified empirically 2026-05-10):
        The subgraph's `price` field equals takerAmountFilled / makerAmountFilled
        and is NOT the implied probability directly — its meaning depends on
        `side`:

            side == "sell":  price = USDC-per-token  →  probability = price
            side == "buy":   price = tokens-per-USDC →  probability = 1 / price

        Use polybot_research.eml_node.data.probability.implied_probability_of_token
        to recover the probability of the fill's own token, and
        implied_yes_probability(fill, is_yes_token=...) to recover the YES
        probability from either side.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    transaction_hash: str
    timestamp: int  # Unix seconds
    block_number: int
    order_hash: str
    maker: str
    taker: str
    maker_asset_id: str
    taker_asset_id: str
    # Raw amounts in smallest unit (10^-6 USDC for collateral; 10^-6 token
    # for the conditional outcome token). Source of truth for any derived
    # price. Subgraph returns these as BigInt (string); we coerce to int
    # via the parser in subgraph.py.
    maker_amount_filled: int
    taker_amount_filled: int
    price: float  # subgraph's reported price — DO NOT trust as probability, see class docstring
    side: str  # "buy" / "sell" — lowercase per empirical observation
    fee: int  # smallest unit


class MarketFillSeries(BaseModel):
    """
    A resolved market joined to its full fill history.
    Output of Phase 0; input to Phase 1 baselines and Phase 3 EML-NODE training.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    market: ResolvedMarket
    fills_yes: list[Fill] = Field(default_factory=list)
    fills_no: list[Fill] = Field(default_factory=list)
