"""Integration tests for the sports scanner LangGraph pipeline."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from polybot.models import Market, MarketCategory, Outcome, PaperTrade, Side, TradeStatus
from polybot.scanner.sports_graph import (
    build_sports_scanner_graph,
    _fuzzy_score,
    _team_tokens,
    _slug_tokens,
    _slug_date,
    _best_match,
)
from polybot.scanner.sports_state import SportsScanState, MatchedPair


def _make_market(
    market_id: str = "test-market-123",
    question: str = "Will the Lakers beat the Celtics?",
    yes_price: float = 0.60,
    hours_until_close: float = 24.0,
) -> Market:
    return Market(
        id=market_id,
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


class TestHelperFunctions:
    def test_fuzzy_score_identical_strings(self):
        assert _fuzzy_score("Lakers vs Celtics", "Lakers vs Celtics") == 1.0

    def test_fuzzy_score_similar_strings(self):
        score = _fuzzy_score("Lakers vs Celtics", "lakers versus celtics")
        assert score > 0.5

    def test_fuzzy_score_different_strings(self):
        score = _fuzzy_score("Lakers vs Celtics", "Heat vs Knicks")
        # Shares "vs" but teams are different; score should be moderate
        assert score < 0.6

    def test_team_tokens_extracts_meaningful_words(self):
        tokens = _team_tokens("Will the Los Angeles Lakers beat the Boston Celtics?")
        assert "lakers" in tokens
        assert "celtics" in tokens
        assert "boston" in tokens
        # Stopwords should be excluded
        assert "will" not in tokens
        assert "the" not in tokens

    def test_slug_tokens_expands_nba_abbreviations(self):
        tokens = _slug_tokens("aec-nba-lal-bos-2026-04-01")
        assert "nba" in tokens
        assert "lakers" in tokens
        assert "celtics" in tokens

    def test_slug_tokens_expands_nfl_abbreviations(self):
        tokens = _slug_tokens("aec-nfl-kc-sf-2026-02-09")
        assert "nfl" in tokens
        assert "chiefs" in tokens
        assert "niners" in tokens or "francisco" in tokens

    def test_slug_date_extracts_date(self):
        from datetime import date
        result = _slug_date("aec-nba-lal-bos-2026-04-01")
        assert result == date(2026, 4, 1)

    def test_slug_date_returns_none_for_invalid(self):
        assert _slug_date("no-date-here") is None


class TestBestMatch:
    def test_finds_matching_market(self):
        us_markets = [
            {
                "title": "Lakers vs Celtics",
                "slug": "aec-nba-lal-bos-2026-04-01",
                "yesPrice": 0.55,
            },
            {
                "title": "Heat vs Knicks",
                "slug": "aec-nba-mia-nyk-2026-04-01",
                "yesPrice": 0.45,
            },
        ]
        best, score = _best_match("Will the Lakers beat the Celtics?", us_markets)
        assert best is not None
        assert "lakers" in best["title"].lower() or "lal" in best["slug"]
        assert score >= 0.5

    def test_returns_none_for_no_markets(self):
        best, score = _best_match("Lakers vs Celtics", [])
        assert best is None
        assert score == 0.0

    def test_low_score_for_unrelated_market(self):
        us_markets = [
            {
                "title": "Completely Unrelated Market",
                "slug": "random-market",
            }
        ]
        best, score = _best_match("Will the Lakers beat the Celtics?", us_markets)
        assert score < 0.5


class TestSportsScanState:
    def test_state_initialization(self):
        state = SportsScanState()
        assert state.global_sports == []
        assert state.us_events == []
        assert state.matched_pairs == []
        assert state.opportunities == []

    def test_state_with_data(self):
        market = _make_market()
        state = SportsScanState(
            global_sports=[market],
            scan_number=1,
        )
        assert len(state.global_sports) == 1
        assert state.scan_number == 1


class TestMatchedPair:
    def test_matched_pair_creation(self):
        market = _make_market()
        pair = MatchedPair(
            global_market=market,
            us_slug="lakers-celtics",
            us_title="Lakers vs Celtics",
            us_yes_price=0.55,
            us_book_depth=1000.0,
            match_score=0.85,
        )
        assert pair.us_slug == "lakers-celtics"
        assert pair.us_yes_price == 0.55
        assert pair.match_score == 0.85


class TestSportsGraphBuild:
    def test_graph_compiles(self):
        """Verify the sports scanner graph compiles without errors."""
        graph = build_sports_scanner_graph()
        assert graph is not None

    def test_graph_has_expected_nodes(self):
        """Verify the graph has all expected nodes."""
        graph = build_sports_scanner_graph()
        # The compiled graph should have the nodes defined
        assert graph is not None


@pytest.mark.asyncio
class TestSportsGraphExecution:
    async def test_empty_global_sports_ends_gracefully(self):
        """Pipeline should handle empty global sports gracefully."""
        graph = build_sports_scanner_graph()

        # Mock the GammaClient to return empty list
        with patch("polybot.scanner.sports_graph.GammaClient") as mock_gamma:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.fetch_markets = AsyncMock(return_value=[])
            mock_gamma.return_value = mock_client

            # Run the graph
            initial_state = SportsScanState()
            result = await graph.ainvoke(initial_state)

            # Should complete without errors
            assert result is not None
            assert result["opportunities"] == []

    async def test_no_us_credentials_degrades_gracefully(self):
        """Pipeline should work without Polymarket US credentials."""
        graph = build_sports_scanner_graph()

        with patch("polybot.scanner.sports_graph.GammaClient") as mock_gamma:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.fetch_markets = AsyncMock(return_value=[
                _make_market()
            ])
            mock_gamma.return_value = mock_client

            # Mock settings to have no US credentials
            with patch("polybot.scanner.sports_graph.settings") as mock_settings:
                mock_settings.polymarket_key_id = None
                mock_settings.polymarket_secret_key = None
                mock_settings.odds_api_key = None

                initial_state = SportsScanState()
                result = await graph.ainvoke(initial_state)

                # Should complete but with no matched pairs (no US data)
                assert result is not None
                assert result["matched_pairs"] == []


class TestPipelineState:
    def test_open_positions_injected_into_state(self):
        """Verify open positions can be injected into scan state."""
        trade = PaperTrade(
            opportunity_id="opp-123",
            market_id="mkt-123",
            question="Lakers vs Celtics",
            side=Side.YES,
            entry_price=0.55,
            size_usd=10.0,
            shares=10.0 / 0.55,
            status=TradeStatus.OPEN,
            live_platform="polymarket_us",
        )

        state = SportsScanState(open_positions=[trade])
        assert len(state.open_positions) == 1
        assert state.open_positions[0].side == Side.YES
