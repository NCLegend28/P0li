from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated
import secrets
from uuid import uuid4

from loguru import logger
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
        if self.outcomes:
            logger.debug(f"Market {self.id!r} has no YES outcome; using first outcome price")
            return self.outcomes[0].price
        logger.warning(f"Market {self.id!r} has no outcomes — returning 0.0")
        return 0.0  # filtered out by 0.07 <= yes_price <= 0.93 check

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

    @computed_field
    @property
    def edge_pct(self) -> str:
        return f"{self.edge * 100:.1f}%"


# ─── Paper Trade ──────────────────────────────────────────────────────────────

class PaperTrade(BaseModel):
    """A simulated position."""
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