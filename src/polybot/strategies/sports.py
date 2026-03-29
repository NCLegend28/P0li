"""
Sports strategy engine — cross-platform price discrepancy.

The edge is the gap between global Polymarket prices (smart money consensus,
Layer 1) and US Polymarket prices (retail-heavy, thinner liquidity, Layer 3).

Three-layer architecture:
  Layer 1 — Global Polymarket (Gamma API): primary signal, $700M+ volume
  Layer 2 — The Odds API: secondary confirmation from 15+ sportsbooks
  Layer 3 — Polymarket US: execution target, where we actually trade

Edge formula: global_price - us_price
  Positive = US is underpriced relative to global smart money → BUY YES on US
  Negative = US is overpriced relative to global smart money → BUY NO on US

Analogy: same structure as the weather bot.
  Open-Meteo says "38%" but market says 12¢ = edge.
  Global Poly says "65%" but US says 58¢ = same structure, same edge.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from polybot.api.espn import Game, InjuryReport
from polybot.api.odds import GameOdds
from polybot.models import MarketCategory, Market, Opportunity, Side


# ─── Edge computation ─────────────────────────────────────────────────────────

def compute_edge(global_price: float, us_price: float) -> float:
    """
    Primary edge: global consensus vs US execution price.
    Positive = US underpriced (buy YES); negative = US overpriced (buy NO).
    """
    return round(global_price - us_price, 6)


def compute_confirmed_edge(
    global_price: float,
    us_price: float,
    sportsbook_prob: float | None,  # Layer 2, None if no Odds API data
) -> tuple[float, float]:
    """
    Returns (edge, confidence).

    confidence:
      1.0 → global AND sportsbooks agree vs US (highest conviction)
      0.7 → global alone disagrees with US (no sportsbook data available)
      0.5 → global and sportsbooks conflict each other (use average)
    """
    raw_edge = global_price - us_price

    if sportsbook_prob is None:
        return round(raw_edge, 6), 0.7

    books_agree = abs(global_price - sportsbook_prob) < 0.03  # within 3 cents
    if books_agree:
        return round(raw_edge, 6), 1.0

    # Layers 1 and 2 disagree — average them as the consensus estimate
    avg_consensus = (global_price + sportsbook_prob) / 2
    return round(avg_consensus - us_price, 6), 0.5


def devig_odds(home_implied: float, away_implied: float) -> tuple[float, float]:
    """
    Remove bookmaker vig from raw implied probabilities.
    Raw probs sum to ~1.05; true probs sum to 1.0.
    """
    total = home_implied + away_implied
    if total <= 0:
        return 0.5, 0.5
    return round(home_implied / total, 4), round(away_implied / total, 4)


# ─── Entry thresholds ─────────────────────────────────────────────────────────

def _should_trade(
    edge: float,
    confidence: float,
    min_edge: float = 0.05,
) -> bool:
    """
    Entry gate. Two valid regimes:
      - edge >= 5¢ AND confidence >= 0.7 (global alone sufficient)
      - edge >= 3¢ AND confidence == 1.0 (all three layers agree)
    """
    if edge >= min_edge and confidence >= 0.7:
        return True
    if edge >= 0.03 and confidence == 1.0:
        return True
    return False


# ─── Strategy ─────────────────────────────────────────────────────────────────

class SportsStrategy:
    """
    Finds mispricings between global Polymarket (Layer 1) and
    US Polymarket (Layer 3), confirmed by sportsbooks (Layer 2).

    Input: MatchedPair (global ↔ US same game) + optional GameOdds + injuries
    Output: Opportunity objects targeting the US platform
    """

    def __init__(self, min_edge: float = 0.05):
        self._min_edge = min_edge

    def evaluate(
        self,
        global_market: Market,            # Layer 1: global price + metadata
        us_slug: str,                     # Layer 3: US market slug
        us_yes_price: float,              # Layer 3: US YES price
        us_book_depth: float = 0.0,       # US order book depth (USD)
        odds_data: GameOdds | None = None,  # Layer 2: sportsbook confirmation
        injuries: list[InjuryReport] | None = None,
        today_games: list[Game] | None = None,
        yesterday_games: list[Game] | None = None,
        position_size_usd: float = 10.0,
    ) -> Opportunity | None:
        """
        Evaluate a matched global ↔ US market pair for a trading opportunity.

        Returns an Opportunity if edge + confidence thresholds are met.
        Returns None if no edge or conditions not met.
        """
        global_yes = global_market.yes_price
        hours_left = global_market.hours_until_close

        # ── Pre-game lock: don't enter within 2h of game time ─────────────────
        if hours_left < 2.0:
            logger.debug(
                "Pre-game lock: {:.1f}h to close for {}", hours_left, us_slug
            )
            return None

        # ── Primary edge (Layer 1 vs Layer 3) ─────────────────────────────────
        sportsbook_prob: float | None = None
        if odds_data is not None:
            # Match the global market question to home/away probability
            # The global market is typically "Will X beat Y?" where X = home team
            q_lower = global_market.question.lower()
            if odds_data.home_team.lower() in q_lower:
                sportsbook_prob = odds_data.home_prob
            elif odds_data.away_team.lower() in q_lower:
                sportsbook_prob = odds_data.away_prob

        edge, confidence = compute_confirmed_edge(global_yes, us_yes_price, sportsbook_prob)

        # Determine trade direction
        # Positive edge = YES is underpriced on US → buy YES
        # Negative edge = NO is underpriced on US → buy NO (flip sign, trade other side)
        if abs(edge) < self._min_edge and not (abs(edge) >= 0.03 and confidence == 1.0):
            logger.debug(
                "No edge on {}: global={:.3f} us={:.3f} edge={:.3f} conf={:.1f}",
                us_slug, global_yes, us_yes_price, edge, confidence,
            )
            return None

        if edge > 0:
            # US YES is cheap vs global → BUY YES on US
            side = Side.YES
            trade_price = us_yes_price
            trade_edge = edge
        else:
            # US NO is cheap vs global → BUY NO on US
            side = Side.NO
            trade_price = 1.0 - us_yes_price
            trade_edge = -edge  # magnitude of the no-side edge

        if not _should_trade(trade_edge, confidence, self._min_edge):
            return None

        # ── Signal adjustments ────────────────────────────────────────────────
        notes_parts: list[str] = [
            f"global={global_yes:.3f}",
            f"us={us_yes_price:.3f}",
            f"edge={trade_edge:.3f}",
            f"conf={confidence:.1f}",
        ]

        # Back-to-back penalty (NBA fatigue)
        if today_games and yesterday_games:
            from polybot.api.espn import ESPNClient
            espn = ESPNClient()
            q = global_market.question
            for team_keyword in _extract_team_keywords(q):
                if espn.is_back_to_back(team_keyword, yesterday_games, today_games):
                    notes_parts.append(f"B2B:{team_keyword}")
                    logger.info("B2B detected for {}: {}", us_slug, team_keyword)

        # Key injury flag
        if injuries:
            q_lower = global_market.question.lower()
            flagged = [
                i for i in injuries
                if i.team.lower() in q_lower and i.status.lower() in ("out", "doubtful")
            ]
            if flagged:
                notes_parts.append(f"injuries:{len(flagged)}")
                logger.info(
                    "Injury flag on {}: {}",
                    us_slug,
                    ", ".join(f"{i.player} ({i.status})" for i in flagged),
                )

        # Book depth check
        if us_book_depth > 0 and us_book_depth < position_size_usd * 3:
            logger.debug(
                "Thin US book on {}: depth={:.0f} vs needed={:.0f}",
                us_slug, us_book_depth, position_size_usd * 3,
            )
            return None

        logger.info(
            "SPORTS OPP: {} {} @ {:.3f} | {} | {}",
            side, us_slug, trade_price, " | ".join(notes_parts),
            global_market.question[:50],
        )

        return Opportunity(
            market=global_market,
            side=side,
            market_price=trade_price,
            model_probability=global_yes if side == Side.YES else (1 - global_yes),
            edge=trade_edge,
            confidence=confidence,
            global_price=global_yes,
            us_market_slug=us_slug,
            strategy="sports_cross_platform",
            notes=" | ".join(notes_parts),
        )


def _extract_team_keywords(question: str) -> list[str]:
    """
    Extract likely team-name keywords from a Polymarket question.

    E.g. "Will the Los Angeles Lakers beat the Boston Celtics on Mar 29?"
    → ["Lakers", "Celtics"]

    Simple heuristic: look for capitalized words that aren't common question words.
    """
    _stop = {"will", "the", "beat", "win", "score", "more", "than", "on", "in", "at",
              "vs", "against", "over", "first", "be", "a", "an", "by", "points", "game"}
    words = question.split()
    return [
        w.strip("?.,")
        for w in words
        if w and w[0].isupper() and w.lower().strip("?.,'") not in _stop
    ]


def evaluate_sports_markets(
    matched_pairs: list[dict],   # list of {global_market, us_slug, us_yes_price, us_book_depth}
    odds_by_game: dict[str, GameOdds],   # keyed by us_slug
    injuries: list[InjuryReport],
    today_games: list[Game],
    yesterday_games: list[Game],
    min_edge: float = 0.05,
    position_size_usd: float = 10.0,
) -> list[Opportunity]:
    """
    Batch-evaluate all matched pairs and return tradeable opportunities.
    Called by the sports scanner graph node.
    """
    strategy = SportsStrategy(min_edge=min_edge)
    opportunities: list[Opportunity] = []

    for pair in matched_pairs:
        global_market: Market = pair["global_market"]
        us_slug: str = pair["us_slug"]
        us_price: float = pair["us_yes_price"]
        depth: float = pair.get("us_book_depth", 0.0)

        opp = strategy.evaluate(
            global_market=global_market,
            us_slug=us_slug,
            us_yes_price=us_price,
            us_book_depth=depth,
            odds_data=odds_by_game.get(us_slug),
            injuries=injuries,
            today_games=today_games,
            yesterday_games=yesterday_games,
            position_size_usd=position_size_usd,
        )
        if opp:
            opportunities.append(opp)

    return opportunities
