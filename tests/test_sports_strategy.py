"""Unit tests for the sports trading strategy."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from polybot.models import Market, MarketCategory, Side, Outcome
from polybot.strategies.sports import (
    SportsStrategy,
    MatchedGame,
    compute_edge,
    compute_confirmed_edge,
    devig_odds,
    kelly_size,
    evaluate_sports_markets,
)


def _make_market(
    question: str = "Will the Lakers beat the Celtics?",
    yes_price: float = 0.60,
    hours_until_close: float = 24.0,
) -> Market:
    return Market(
        id="test-market-123",
        question=question,
        liquidity_usd=5000.0,
        volume_usd=10000.0,
        category=MarketCategory.SPORTS,
        end_date=datetime.now(timezone.utc) + timedelta(hours=hours_until_close),
        outcomes=[
            Outcome(name="YES", price=yes_price, clobTokenId="yes-token-123"),
            Outcome(name="NO", price=1.0 - yes_price, clobTokenId="no-token-123"),
        ],
    )


def _make_matched_game(
    global_price: float = 0.60,
    us_price: float = 0.55,
    hours_until_close: float = 24.0,
    status: str = "status_scheduled",
) -> MatchedGame:
    return MatchedGame(
        global_market=_make_market(yes_price=global_price, hours_until_close=hours_until_close),
        us_slug="lakers-celtics-2026-04-01",
        us_yes_price=us_price,
        us_book_depth=1000.0,
        game_start=datetime.now(timezone.utc) + timedelta(hours=hours_until_close),
        status=status,
        home_team="Los Angeles Lakers",
        away_team="Boston Celtics",
    )


class TestComputeEdge:
    def test_positive_edge_when_us_underpriced(self):
        """US YES is cheap vs global -> positive edge -> buy YES."""
        edge = compute_edge(global_price=0.65, us_price=0.58)
        assert edge == pytest.approx(0.07, abs=0.001)

    def test_negative_edge_when_us_overpriced(self):
        """US YES is expensive vs global -> negative edge -> buy NO."""
        edge = compute_edge(global_price=0.50, us_price=0.58)
        assert edge == pytest.approx(-0.08, abs=0.001)

    def test_zero_edge_when_prices_match(self):
        edge = compute_edge(global_price=0.55, us_price=0.55)
        assert edge == 0.0


class TestComputeConfirmedEdge:
    def test_high_confidence_when_sportsbook_agrees(self):
        """Sportsbook within 3 cents of global -> confidence 1.0."""
        edge, conf = compute_confirmed_edge(
            global_price=0.65,
            us_price=0.58,
            sportsbook_prob=0.64,  # within 3 cents of global
        )
        assert edge == pytest.approx(0.07, abs=0.001)
        assert conf == 1.0

    def test_medium_confidence_when_no_sportsbook(self):
        """No sportsbook data -> confidence 0.7."""
        edge, conf = compute_confirmed_edge(
            global_price=0.65,
            us_price=0.58,
            sportsbook_prob=None,
        )
        assert edge == pytest.approx(0.07, abs=0.001)
        assert conf == 0.7

    def test_low_confidence_when_layers_disagree(self):
        """Global and sportsbook disagree -> confidence 0.5, averaged edge."""
        edge, conf = compute_confirmed_edge(
            global_price=0.65,
            us_price=0.58,
            sportsbook_prob=0.55,  # 10 cents from global -> disagree
        )
        # Averaged: (0.65 + 0.55)/2 = 0.60; edge = 0.60 - 0.58 = 0.02
        assert edge == pytest.approx(0.02, abs=0.001)
        assert conf == 0.5


class TestDevigOdds:
    def test_removes_vig_from_overround(self):
        """Raw implied probs sum to ~1.05, devigged should sum to 1.0."""
        home, away = devig_odds(home_implied=0.52, away_implied=0.53)
        assert abs(home + away - 1.0) < 0.001

    def test_preserves_relative_probability(self):
        """Team that was more likely stays more likely."""
        home, away = devig_odds(home_implied=0.60, away_implied=0.45)
        assert home > away

    def test_handles_zero_total(self):
        """Edge case: if both zero, return 50/50."""
        home, away = devig_odds(home_implied=0.0, away_implied=0.0)
        assert home == 0.5
        assert away == 0.5


class TestKellySize:
    def test_returns_minimum_floor(self):
        """Small edge should still return $2 minimum."""
        size = kelly_size(
            edge=0.01,
            price=0.50,
            bankroll=1000.0,
            open_exposure=0.0,
        )
        assert size >= 2.0

    def test_returns_maximum_cap(self):
        """Large edge should be capped at $15."""
        size = kelly_size(
            edge=0.50,  # massive 50% edge
            price=0.50,
            bankroll=10000.0,
            open_exposure=0.0,
        )
        assert size <= 15.0

    def test_respects_exposure_cap(self):
        """40% exposure cap should limit sizing."""
        size = kelly_size(
            edge=0.10,
            price=0.50,
            bankroll=100.0,
            open_exposure=35.0,  # already 35% exposed
        )
        # 40% cap = $40, minus $35 open = $5 max remaining
        assert size <= 5.0

    def test_zero_size_when_fully_exposed(self):
        """Should return 0 when already at exposure cap."""
        size = kelly_size(
            edge=0.10,
            price=0.50,
            bankroll=100.0,
            open_exposure=40.0,  # at the 40% cap
        )
        assert size == 0.0

    def test_invalid_price_returns_zero(self):
        """Price at 0 or 1 should return 0."""
        assert kelly_size(edge=0.10, price=0.0, bankroll=100.0, open_exposure=0.0) == 0.0
        assert kelly_size(edge=0.10, price=1.0, bankroll=100.0, open_exposure=0.0) == 0.0


class TestSportsStrategy:
    def test_finds_opportunity_when_edge_exists(self):
        """Should return opportunity when US is underpriced vs global."""
        strategy = SportsStrategy(min_edge=0.05)
        opp = strategy.evaluate(
            global_market=_make_market(yes_price=0.65, hours_until_close=24.0),
            us_slug="lakers-celtics",
            us_yes_price=0.58,  # 7 cents cheap
            us_book_depth=1000.0,
        )
        assert opp is not None
        assert opp.side == Side.YES  # buy the underpriced YES
        assert opp.edge == pytest.approx(0.07, abs=0.01)

    def test_rejects_when_edge_too_small(self):
        """Should return None when edge below threshold."""
        strategy = SportsStrategy(min_edge=0.05)
        opp = strategy.evaluate(
            global_market=_make_market(yes_price=0.60, hours_until_close=24.0),
            us_slug="lakers-celtics",
            us_yes_price=0.58,  # only 2 cents edge
        )
        assert opp is None

    def test_pregame_lock_blocks_entry(self):
        """Should not enter within 2 hours of game time."""
        strategy = SportsStrategy(min_edge=0.05)
        opp = strategy.evaluate(
            global_market=_make_market(yes_price=0.65, hours_until_close=1.5),  # <2h
            us_slug="lakers-celtics",
            us_yes_price=0.58,
        )
        assert opp is None

    def test_buys_no_when_us_overpriced(self):
        """Should buy NO when US YES is expensive vs global."""
        strategy = SportsStrategy(min_edge=0.05)
        opp = strategy.evaluate(
            global_market=_make_market(yes_price=0.50, hours_until_close=24.0),
            us_slug="lakers-celtics",
            us_yes_price=0.58,  # US YES overpriced -> buy NO
        )
        assert opp is not None
        assert opp.side == Side.NO

    def test_thin_book_rejected(self):
        """Should reject when book depth too thin for position size."""
        strategy = SportsStrategy(min_edge=0.05)
        opp = strategy.evaluate(
            global_market=_make_market(yes_price=0.65, hours_until_close=24.0),
            us_slug="lakers-celtics",
            us_yes_price=0.58,
            us_book_depth=10.0,  # too thin for $15 position
            position_size_usd=15.0,
        )
        assert opp is None


class TestEvaluateSportsMarkets:
    def test_batch_evaluation_filters_non_scheduled(self):
        """Should skip games that are in-progress or final."""
        pairs = [
            _make_matched_game(global_price=0.65, us_price=0.58, status="status_scheduled"),
            _make_matched_game(global_price=0.65, us_price=0.58, status="status_in_progress"),
            _make_matched_game(global_price=0.65, us_price=0.58, status="status_final"),
        ]
        opps = evaluate_sports_markets(
            matched_pairs=pairs,
            odds_by_game={},
            injuries=[],
            today_games=[],
            yesterday_games=[],
            bankroll=1000.0,
            min_edge=0.05,
        )
        # Only the scheduled one should produce an opportunity
        assert len(opps) == 1

    def test_empty_pairs_returns_empty(self):
        opps = evaluate_sports_markets(
            matched_pairs=[],
            odds_by_game={},
            injuries=[],
            today_games=[],
            yesterday_games=[],
        )
        assert opps == []

    def test_kelly_sizing_applied(self):
        """Each opportunity should have a size_usd set via Kelly."""
        pairs = [
            _make_matched_game(global_price=0.70, us_price=0.60),  # 10 cent edge
        ]
        opps = evaluate_sports_markets(
            matched_pairs=pairs,
            odds_by_game={},
            injuries=[],
            today_games=[],
            yesterday_games=[],
            bankroll=1000.0,
            min_edge=0.05,
        )
        assert len(opps) == 1
        assert opps[0].size_usd >= 2.0  # minimum floor
        assert opps[0].size_usd <= 15.0  # maximum cap
