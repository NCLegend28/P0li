"""
Sportsbook delay arbitrage strategy.

Detects when sportsbook odds move faster than Polymarket US prices,
creating a temporary edge before the US market catches up.

Key insight: Sportsbooks adjust prices in real-time based on:
- Breaking news (injuries, lineup changes)
- Sharp money hitting the books
- Live game events (for in-game)

Polymarket US updates slower (manual or delayed), creating tradeable windows.

This module tracks odds movements and generates signals when:
1. Sportsbook odds shift significantly (> threshold)
2. US Polymarket hasn't updated yet (stale price)
3. Edge exceeds minimum threshold

Independently run alongside other strategies. Filters out overlapping
opportunities to avoid duplicate trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from collections import defaultdict

from loguru import logger

from polybot.api.odds import GameOdds
from polybot.config import settings
from polybot.models import Market, MarketCategory, Opportunity, Outcome, Side
from polybot.strategies.us_direct import USEvent, devig_odds


@dataclass
class OddsMovement:
    """Tracks an odds change event."""
    timestamp: datetime
    book_prob: float
    us_price: float
    edge: float
    triggered: bool = False


@dataclass
class DelayArbState:
    """State for a tracked event."""
    event_slug: str
    home_team: str
    away_team: str
    sport: str
    movements: list[OddsMovement] = field(default_factory=list)
    last_book_prob: Optional[float] = None
    last_us_price: Optional[float] = None
    cooldown_until: Optional[datetime] = None


class DelayArbitrageStrategy:
    """
    Sportsbook delay arbitrage.
    
    Monitors for significant odds movements that haven't propagated
to Polymarket US yet. Generates signals during the delay window.
    
    Independent of other strategies. Filters overlaps.
    """
    
    def __init__(
        self,
        min_edge: float = 0.04,
        min_movement: float = 0.03,  # Book must move 3¢+ to trigger
        cooldown_minutes: float | None = None,  # Uses settings.delay_arb_cooldown_minutes if None
        max_delay_seconds: float = 120.0,  # Book-US max acceptable staleness
    ):
        self._min_edge = min_edge
        self._min_movement = min_movement
        self._cooldown = cooldown_minutes if cooldown_minutes is not None else settings.delay_arb_cooldown_minutes
        self._max_delay = max_delay_seconds
        self._state: dict[str, DelayArbState] = {}
        self._executed_slugs: set[str] = set()  # Avoid dupes with other strategies
    
    def evaluate(
        self,
        us_event: USEvent,
        sportsbook_odds: GameOdds | None,
        existing_opportunities: list[str],  # IDs to exclude
        bankroll: float,
        open_exposure: float,
    ) -> Optional[Opportunity]:
        """
        Evaluate a single US event for delay arbitrage.
        
        Returns Opportunity if conditions met, None otherwise.
        """
        
        slug = us_event.slug

        if not settings.delay_arb_enabled:
            logger.debug("[DELAY_ARB] Disabled via config; skipping slug=%s", slug)
            return None
        # Skip if already in existing opportunities (cross-strategy dedupe)
        if any(slug in opp_id for opp_id in existing_opportunities):
            logger.debug("[DELAY_ARB] skipping slug=%s — already in existing opps", slug)
            return None
        
        # Skip if no sportsbook data
        if not sportsbook_odds:
            return None
        
        # Skip if in cooldown
        state = self._state.get(slug)
        if state and state.cooldown_until and datetime.now(timezone.utc) < state.cooldown_until:
            logger.debug("[DELAY_ARB] slug=%s in cooldown until %s", slug, state.cooldown_until)
            return None
        
        # Calculate current probabilities
        home_implied = 1.0 / sportsbook_odds.home_odds if sportsbook_odds.home_odds > 0 else 0
        away_implied = 1.0 / sportsbook_odds.away_odds if sportsbook_odds.away_odds > 0 else 0
        home_prob, away_prob = devig_odds(home_implied, away_implied)
        
        # Determine which side corresponds to YES
        # Assume YES = home team for most markets
        book_prob = home_prob
        us_price = us_event.yes_price
        
        # Check for significant movement
        if state and state.last_book_prob:
            book_delta = abs(book_prob - state.last_book_prob)
            if book_delta < self._min_movement:
                logger.debug("[DELAY_ARB] slug=%s book moved only %.3f < %.3f threshold", slug, book_delta, self._min_movement)
                return None
            
            # Check if US has updated (converged)
            us_delta = abs(us_price - state.last_us_price) if state.last_us_price else 0
            if us_delta > book_delta * 0.5:  # US moved at least half as much
                logger.debug("[DELAY_ARB] slug=%s US already converged (us_delta=%.3f)", slug, us_delta)
                return None
            
            # MOVEMENT DETECTED — calculate edge
            edge = book_prob - us_price
            abs_edge = abs(edge)
            
            if abs_edge < self._min_edge:
                logger.debug("[DELAY_ARB] slug=%s edge %.3f < %.3f threshold", slug, abs_edge, self._min_edge)
                return None
            
            # SUCCESS — create opportunity
            side = Side.YES if edge > 0 else Side.NO
            market_price = us_price if side == Side.YES else us_event.no_price

            # Build a minimal Market so Opportunity validates correctly
            synthetic_market = Market(
                id=slug,
                question=us_event.title,
                category=MarketCategory.SPORTS,
                end_date=us_event.game_start,
                liquidity_usd=us_event.volume,
                volume_usd=us_event.volume,
                outcomes=[
                    Outcome(name="YES", price=us_price,             clobTokenId=""),
                    Outcome(name="NO",  price=round(1 - us_price, 4), clobTokenId=""),
                ],
            )

            opp = Opportunity(
                id=f"delay_arb_{slug}_{int(datetime.now(timezone.utc).timestamp())}",
                market=synthetic_market,
                side=side,
                model_probability=book_prob if side == Side.YES else (1 - book_prob),
                market_price=market_price,
                edge=abs_edge,
                confidence=0.80,  # Slightly lower than direct (time-sensitive)
                us_market_slug=slug,
                strategy="delay_arb",
                notes=f"Delay arb: book moved {book_delta:.3f} → {book_prob:.3f}, "
                      f"US stale at {us_price:.3f}, edge {abs_edge:.3f}",
            )
            
            # Set cooldown
            state.cooldown_until = datetime.now(timezone.utc) + timedelta(
                minutes=self._cooldown
            )
            state.movements.append(OddsMovement(
                timestamp=datetime.now(timezone.utc),
                book_prob=book_prob,
                us_price=us_price,
                edge=abs_edge,
                triggered=True,
            ))
            
            logger.info(
                "[DELAY_ARB] NEW SIGNAL: %s | book moved %.3f → %.3f, US %.3f | %s edge %.3f",
                us_event.title[:45], book_delta, book_prob, us_price, side.value, abs_edge
            )
            
            return opp
        
        # First observation — initialize state
        if not state:
            self._state[slug] = DelayArbState(
                event_slug=slug,
                home_team=us_event.home_team,
                away_team=us_event.away_team,
                sport=us_event.sport,
            )
            state = self._state[slug]
        
        # Update tracking
        state.last_book_prob = book_prob
        state.last_us_price = us_price
        state.movements.append(OddsMovement(
            timestamp=datetime.now(timezone.utc),
            book_prob=book_prob,
            us_price=us_price,
            edge=abs(book_prob - us_price),
            triggered=False,
        ))
        
        # Cleanup old state
        self._cleanup_old_state()
        
        return None
    
    def evaluate_batch(
        self,
        us_events: list[USEvent],
        odds_by_game: dict[str, GameOdds],
        existing_opportunities: list[str],
        bankroll: float,
        open_exposure: float,
    ) -> list[Opportunity]:
        """
        Evaluate batch of US events for delay arbitrage.
        
        Returns list of opportunities (usually 0-2 per scan).
        """
        opportunities = []
        if not settings.delay_arb_enabled:
            logger.debug("[DELAY_ARB] Disabled via config; skipping batch evaluation")
            return []

        for event in us_events:
            odds = odds_by_game.get(event.slug)
            
            opp = self.evaluate(
                us_event=event,
                sportsbook_odds=odds,
                existing_opportunities=existing_opportunities,
                bankroll=bankroll,
                open_exposure=open_exposure,
            )
            
            if opp:
                opportunities.append(opp)
        
        logger.info("[DELAY_ARB] Batch: %d events → %d delay opportunities", len(us_events), len(opportunities))
        return opportunities
    
    def _cleanup_old_state(self, max_age_hours: float = 24.0) -> None:
        """Remove stale tracking data."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        stale = [
            slug for slug, state in self._state.items()
            if state.movements and state.movements[-1].timestamp < cutoff
        ]
        for slug in stale:
            del self._state[slug]


