"""
State flowing through the sports scanner LangGraph pipeline.

Three-layer architecture:
  Layer 1: Global Polymarket (Gamma API) — smart money prices, read-only
  Layer 2: The Odds API — sportsbook confirmation (optional)
  Layer 3: Polymarket US (SDK) — execution target, where we trade
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from polybot.api.espn import Game, InjuryReport
from polybot.api.odds import GameOdds
from polybot.models import Market, Opportunity, PaperTrade
from polybot.strategies.exit import ExitSignal


@dataclass
class MatchedPair:
    """
    A global Gamma market matched to its Polymarket US equivalent.

    This is the core data structure of the sports edge signal:
      - global_market.yes_price = Layer 1 (smart money consensus)
      - us_yes_price             = Layer 3 (US retail market)
      - edge = global_price - us_price
    """
    global_market: Market         # Layer 1: global market with price + metadata
    us_slug: str                  # Layer 3: US platform slug (e.g. "lakers-celtics")
    us_title: str                 # US market display title
    us_yes_price: float           # Layer 3: current YES price on US platform
    us_book_depth: float = 0.0    # approximate USD depth on US order book
    match_score: float = 0.0      # fuzzy match quality 0.0–1.0
    us_status: str = "status_scheduled"  # from US event dict (closed/active flag)


class SportsScanState(BaseModel):
    """
    Immutable-by-convention state flowing through the sports scanner graph.
    Each node returns a dict that gets merged into this state.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ── Layer 1: Global consensus prices (Gamma API, read-only) ──────────────
    global_sports: list[Market] = Field(default_factory=list)

    # ── Layer 3: US execution targets (Polymarket US SDK) ────────────────────
    us_events: list[dict] = Field(default_factory=list)

    # ── Matched pairs (global ↔ US for same game) ─────────────────────────────
    matched_pairs: list[MatchedPair] = Field(default_factory=list)

    # ── Layer 2: Sportsbook confirmation (optional — 500 req/month free) ──────
    odds_data: list[GameOdds] = Field(default_factory=list)

    # ── Support data ──────────────────────────────────────────────────────────
    injuries: list[InjuryReport] = Field(default_factory=list)
    today_games: list[Game] = Field(default_factory=list)
    yesterday_games: list[Game] = Field(default_factory=list)

    # ── Strategy output ───────────────────────────────────────────────────────
    opportunities: list[Opportunity] = Field(default_factory=list)
    exit_signals: list[ExitSignal] = Field(default_factory=list)

    # ── Injected by CLI before each scan ─────────────────────────────────────
    open_positions: list[PaperTrade] = Field(default_factory=list)

    # ── Metadata ─────────────────────────────────────────────────────────────
    scan_number: int = 0
    errors: list[str] = Field(default_factory=list)
