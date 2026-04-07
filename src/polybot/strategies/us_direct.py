"""
Direct US Polymarket trading strategy.

Trades purely on US platform markets without requiring cross-platform arbitrage.
Uses Layer 2 (sportsbook odds) as the signal instead of global Polymarket.

Edge formula: sportsbook_probability - us_price
  Positive = US is underpriced vs books → BUY YES
  Negative = US is overpriced vs books → BUY NO

This allows trading on US-only markets (no global equivalent) and avoids
the geoblock issues with global CLOB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from polybot.api.odds import GameOdds
from polybot.config import settings
from polybot.models import Market, MarketCategory, Opportunity, Outcome, Side


@dataclass
class USEvent:
    """US Polymarket event with odds data."""
    slug: str
    title: str
    yes_price: float
    no_price: float
    volume: float
    game_start: datetime
    sport: str
    home_team: str
    away_team: str


def devig_odds(home_implied: float, away_implied: float) -> tuple[float, float]:
    """Remove bookmaker vig from raw implied probabilities."""
    total = home_implied + away_implied
    if total <= 0:
        return 0.5, 0.5
    return round(home_implied / total, 4), round(away_implied / total, 4)


def compute_us_edge(us_price: float, sportsbook_prob: float) -> float:
    """
    Edge between US price and sportsbook consensus.
    Positive = US underpriced (buy YES)
    Negative = US overpriced (buy NO, edge is absolute value)
    """
    return round(sportsbook_prob - us_price, 6)


def kelly_size_us(
    edge: float,
    price: float,
    bankroll: float,
    open_exposure: float,
    fraction: float = 0.15,
) -> float:
    """Kelly sizing for US direct trades."""
    if price <= 0 or price >= 1:
        return 0.0
    
    # Full Kelly = edge / (1 - price) for long shots, edge/price for favorites
    # Use conservative half-Kelly
    q = 1.0 - price
    if edge > 0:
        # Buying YES
        win_prob = price + edge
        full_kelly = win_prob - ((1 - win_prob) * price / q)
    else:
        # Buying NO
        win_prob = q - abs(edge)
        full_kelly = win_prob - ((1 - win_prob) * q / price)
    
    raw = bankroll * full_kelly * fraction
    raw = max(2.0, min(raw, settings.live_sports_max_position_usd))
    
    # 40% exposure cap
    remaining = (bankroll * 0.40) - open_exposure
    return min(raw, max(remaining, 0.0))


def evaluate_us_direct(
    us_event: USEvent,
    sportsbook_odds: GameOdds | None,
    bankroll: float,
    open_exposure: float,
    min_edge: float = 0.05,
) -> Optional[Opportunity]:
    """
    Evaluate a US event for direct trading opportunity.
    
    Returns Opportunity if edge > threshold, None otherwise.
    """
    if not sportsbook_odds:
        logger.debug(f"US direct: no odds for {us_event.slug}")
        return None
    
    # Determine which team is the "YES" outcome
    # Most US markets are "Will [Home Team] win?" or similar
    home_implied = 1.0 / sportsbook_odds.home_odds if sportsbook_odds.home_odds > 0 else 0
    away_implied = 1.0 / sportsbook_odds.away_odds if sportsbook_odds.away_odds > 0 else 0
    
    # Devig to get true probabilities
    home_prob, away_prob = devig_odds(home_implied, away_implied)
    
    # Assume YES = home team wins for most markets
    # Edge calculation
    edge = compute_us_edge(us_event.yes_price, home_prob)
    
    # Check if we should trade
    abs_edge = abs(edge)
    if abs_edge < min_edge:
        logger.debug(f"US direct: edge {abs_edge:.3f} < {min_edge} for {us_event.slug}")
        return None
    
    # Determine side
    side = Side.YES if edge > 0 else Side.NO
    market_price = us_event.yes_price if side == Side.YES else us_event.no_price
    
    # Kelly sizing
    size_usd = kelly_size_us(
        edge=edge,
        price=market_price,
        bankroll=bankroll,
        open_exposure=open_exposure,
    )
    
    if size_usd < 2.0:
        logger.debug(f"US direct: size ${size_usd:.2f} too small for {us_event.slug}")
        return None
    
    # Build a minimal Market so Opportunity validates correctly
    synthetic_market = Market(
        id=us_event.slug,
        question=us_event.title,
        category=MarketCategory.SPORTS,
        end_date=us_event.game_start,
        liquidity_usd=us_event.volume,
        volume_usd=us_event.volume,
        outcomes=[
            Outcome(name="YES", price=us_event.yes_price, clobTokenId=""),
            Outcome(name="NO",  price=us_event.no_price,  clobTokenId=""),
        ],
    )

    # Create opportunity
    opp = Opportunity(
        id=f"us_direct_{us_event.slug}",
        market=synthetic_market,
        side=side,
        model_probability=home_prob if side == Side.YES else away_prob,
        market_price=market_price,
        edge=abs_edge,
        confidence=0.85,  # Sportsbook-based
        us_market_slug=us_event.slug,
        strategy="us_direct",
        notes=f"US direct: {side.value} @ {market_price:.3f}, book prob {home_prob:.3f}, edge {edge:.3f}",
    )
    
    logger.info(
        f"US DIRECT OPPORTUNITY: {us_event.title[:50]} | "
        f"{side.value} @ {market_price:.3f} | edge {abs_edge:.3f} | size ${size_usd:.2f}"
    )
    
    return opp


class USDirectStrategy:
    """
    Direct trading strategy for Polymarket US.
    
    Trades on price discrepancies between US markets and
    sportsbook consensus (Layer 2 confirmation).
    """
    
    def __init__(self, min_edge: float = 0.05):
        self._min_edge = min_edge
    
    def evaluate_batch(
        self,
        us_events: list[USEvent],
        odds_by_game: dict[str, GameOdds],
        bankroll: float,
        open_exposure: float,
    ) -> list[Opportunity]:
        """
        Evaluate batch of US events for trading opportunities.
        
        Returns list of Opportunity objects for execution.
        """
        opportunities = []
        
        for event in us_events:
            # Match event to odds by team name
            odds = self._match_event_to_odds(event, odds_by_game)
            
            opp = evaluate_us_direct(
                us_event=event,
                sportsbook_odds=odds,
                bankroll=bankroll,
                open_exposure=open_exposure,
                min_edge=self._min_edge,
            )
            
            if opp:
                opportunities.append(opp)
        
        logger.info(f"US direct strategy: {len(us_events)} events → {len(opportunities)} opportunities")
        return opportunities
    
    def _match_event_to_odds(
        self,
        event: USEvent,
        odds_by_game: dict[str, GameOdds],
    ) -> Optional[GameOdds]:
        """Match US event to sportsbook odds by team names."""
        event_teams = set(
            (event.home_team or "").lower().split() + 
            (event.away_team or "").lower().split()
        )
        
        for slug, odds in odds_by_game.items():
            odds_teams = set(
                (odds.home_team or "").lower().split() + 
                (odds.away_team or "").lower().split()
            )
            
            # Simple overlap matching
            if event_teams & odds_teams:
                return odds
        
        return None
