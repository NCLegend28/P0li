from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value

from typing import Annotated
import secrets
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field


# ─── Enums ────────────────────────────────────────────────────────────────────

class MarketCategory(StrEnum):
    WEATHER     = "weather"
    CRYPTO      = "crypto"
    POLITICS    = "politics"
    SPORTS      = "sports"
    OTHER       = "other"


class Side(StrEnum):
    YES = "YES"
    NO  = "NO"


class TradeStatus(StrEnum):
    OPEN    = "open"
    CLOSED  = "closed"


# ─── Market ───────────────────────────────────────────────────────────────────

class Outcome(BaseModel):
    """A single YES/NO outcome within a market."""
    name:         str
    price:        float   # 0.0 – 1.0  (implied probability)
    clobTokenId:  str     = ""


class Market(BaseModel):
    """Normalised Polymarket market as returned by the Gamma API."""
    id:               str
    question:         str
    category:         MarketCategory = MarketCategory.OTHER
    end_date:         datetime
    liquidity_usd:    float
    volume_usd:       float
    outcomes:         list[Outcome]
    active:           bool = True
    closed:           bool = False

    @computed_field
    @property
    def yes_price(self) -> float:
        for o in self.outcomes:
            if o.name.upper() == "YES":
                return o.price
        return self.outcomes[0].price if self.outcomes else 0.5

    @computed_field
    @property
    def no_price(self) -> float:
        return round(1.0 - self.yes_price, 6)

    @computed_field
    @property
    def hours_until_close(self) -> float:
        now = datetime.now(timezone.utc)
        end = self.end_date.replace(tzinfo=timezone.utc) if self.end_date.tzinfo is None else self.end_date
        return max(0.0, (end - now).total_seconds() / 3600)


# ─── Opportunity ──────────────────────────────────────────────────────────────

class Opportunity(BaseModel):
    """An edge detected by a strategy."""
    id:                str           = Field(default_factory=lambda: secrets.token_hex(6))
    market:            Market
    side:              Side
    market_price:      float         # what Polymarket is pricing
    model_probability: float         # what our model estimates
    edge:              float         # model_probability - market_price
    strategy:          str
    detected_at:       datetime      = Field(default_factory=lambda: datetime.now(timezone.utc))
    notes:             str           = ""

    # ── Sports / US platform fields ───────────────────────────────────────────
    us_market_slug: str = ""   # Polymarket US slug (e.g. "lakers-celtics-mar-29")
    global_price:   float | None = None   # Layer 1 consensus price
    confidence:     float = 0.7           # 0.5 / 0.7 / 1.0 from sports strategy
    size_usd:       float = 10.0          # Kelly-sized position in USD

    @computed_field
    @property
    def edge_pct(self) -> str:
        return f"{self.edge * 100:.1f}%"

    @property
    def clob_token_id(self) -> str:
        """Returns the CLOB token ID for the side we're trading (global platform)."""
        for outcome in self.market.outcomes:
            if outcome.name.upper() == str(self.side).upper():
                return outcome.clobTokenId
        return ""


# ─── Live Game Context ────────────────────────────────────────────────────────

class LiveGameContext(BaseModel):
    """Live state of an in-progress game, fetched from ESPN."""
    game_id:           str
    sport:             str       # "NBA", "NFL", "EPL", etc.
    home_team:         str
    away_team:         str
    home_score:        int
    away_score:        int
    period:            int       # quarter/half/set number
    seconds_remaining: float     # total seconds left in the game
    is_final:          bool = False
    fetched_at:        datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def score_diff(self) -> int:
        """Home score minus away score."""
        return self.home_score - self.away_score


# ─── Trade Record ─────────────────────────────────────────────────────────────

class TradeRecord(BaseModel):
    """A trade record — for both simulated and live trades."""
    id:               str      = Field(default_factory=lambda: secrets.token_hex(6))
    opportunity_id:   str
    market_id:        str
    question:         str
    side:             Side
    entry_price:      float
    size_usd:         float
    shares:           float    # size_usd / entry_price
    status:           TradeStatus = TradeStatus.OPEN
    opened_at:        datetime   = Field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at:        datetime | None = None
    exit_price:       float | None    = None
    clob_order_id:    str   | None    = None   # live order ID (global CLOB)
    live_order_id:    str   | None    = None   # live order ID (US platform)
    live_platform:    str   | None    = None   # "polymarket_us" or "polymarket_global"
    clob_token_id:    str   | None    = None   # YES/NO token bought on global CLOB
    us_market_slug:   str   | None    = None   # US platform market slug for close_position

    @computed_field
    @property
    def pnl_usd(self) -> float:
        if self.exit_price is None:
            return 0.0
        return round((self.exit_price - self.entry_price) * self.shares, 4)

    @computed_field
    @property
    def pnl_pct(self) -> float:
        if self.exit_price is None:
            return 0.0
        return round((self.exit_price - self.entry_price) / self.entry_price * 100, 2)