import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

from polybot.strategies.delay_arb import DelayArbitrageStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_mock_usevent(
    slug="us-event-123",
    title="Mock Game Title",
    yes_price=0.5,
    home_team="TeamA",
    away_team="TeamB",
    sport="test_sport",
    game_start=None,
):
    return SimpleNamespace(
        slug=slug,
        title=title,
        yes_price=yes_price,
        no_price=round(1.0 - yes_price, 4),
        volume=1000.0,
        game_start=game_start or datetime.now(timezone.utc) + timedelta(hours=5),
        sport=sport,
        home_team=home_team,
        away_team=away_team,
    )


def create_mock_gameodds(
    home_odds=2.0,
    away_odds=2.0,
    home_team="TeamA",
    away_team="TeamB",
    sport_key="test_sport",
    commence_time=None,
):
    return SimpleNamespace(
        home_odds=home_odds,
        away_odds=away_odds,
        home_team=home_team,
        away_team=away_team,
        sport_key=sport_key,
        commence_time=commence_time or datetime.now(timezone.utc) + timedelta(hours=5),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDelayArbitrageStrategy(unittest.TestCase):

    def setUp(self):
        # Patch settings in the strategy module directly so evaluate() sees it
        self.settings_patcher = patch("polybot.strategies.delay_arb.settings")
        self.mock_settings = self.settings_patcher.start()
        self.mock_settings.delay_arb_enabled = True
        self.mock_settings.sports_min_edge = 0.05
        self.mock_settings.live_sports_max_position_usd = 8.0
        self.mock_settings.simulated_starting_balance = 1000.0
        self.mock_settings.live_trading = True
        self.mock_settings.sports_max_daily_loss = 50.0

        self.strategy = DelayArbitrageStrategy(
            min_edge=0.04,
            min_movement=0.03,
            cooldown_minutes=0.5,  # Short cooldown for testing
        )
        self.strategy._state = {}
        self.strategy._executed_slugs = set()

    def tearDown(self):
        self.settings_patcher.stop()

    # ------------------------------------------------------------------
    # Disabled strategy
    # ------------------------------------------------------------------

    def test_delay_arb_disabled(self):
        self.mock_settings.delay_arb_enabled = False
        event = create_mock_usevent()
        odds = create_mock_gameodds()
        opportunities = self.strategy.evaluate_batch(
            us_events=[event],
            odds_by_game={event.slug: odds},
            existing_opportunities=[],
            bankroll=1000,
            open_exposure=0,
        )
        self.assertEqual(len(opportunities), 0, "Should return no opportunities when disabled")

    # ------------------------------------------------------------------
    # Missing data
    # ------------------------------------------------------------------

    def test_no_odds_data(self):
        event = create_mock_usevent()
        opportunities = self.strategy.evaluate_batch(
            us_events=[event],
            odds_by_game={},  # No odds
            existing_opportunities=[],
            bankroll=1000,
            open_exposure=0,
        )
        self.assertEqual(len(opportunities), 0, "Should return no opportunities if no odds data")

    # ------------------------------------------------------------------
    # Movement / edge threshold checks
    # ------------------------------------------------------------------

    def test_no_movement(self):
        """Second observation with same odds => no movement => no opportunity."""
        event = create_mock_usevent(yes_price=0.5)
        odds = create_mock_gameodds(home_odds=2.0, away_odds=2.0)

        # First call: initialize state
        self.strategy.evaluate(event, odds, [], 1000, 0)

        # Second call: same odds, no movement
        opportunities = self.strategy.evaluate_batch(
            us_events=[event],
            odds_by_game={event.slug: odds},
            existing_opportunities=[],
            bankroll=1000,
            open_exposure=0,
        )
        self.assertEqual(len(opportunities), 0, "Should return no opportunities if no book movement")

    def test_movement_below_threshold(self):
        """Book moves, but not enough to cross min_movement."""
        event = create_mock_usevent(yes_price=0.5)
        # Initial: ~50.3% implied home after devig
        odds_initial = create_mock_gameodds(home_odds=1.9, away_odds=2.0)
        # Slight nudge: ~50.0% implied home after devig — delta well below 0.03
        odds_slight_move = create_mock_gameodds(home_odds=1.95, away_odds=1.96)

        self.strategy.evaluate(event, odds_initial, [], 1000, 0)  # init state

        opportunities = self.strategy.evaluate_batch(
            us_events=[event],
            odds_by_game={event.slug: odds_slight_move},
            existing_opportunities=[],
            bankroll=1000,
            open_exposure=0,
        )
        self.assertEqual(
            len(opportunities), 0,
            "Should return no opportunities if movement is below min_movement",
        )

    def test_edge_below_threshold(self):
        """Book moves enough, but resulting edge vs US price is too small."""
        # US price at 55c; book prob ~55.6% after devig — edge ~0.006, below 0.04
        event = create_mock_usevent(yes_price=0.55)
        odds_initial = create_mock_gameodds(home_odds=2.0, away_odds=2.0)
        odds_slight_edge = create_mock_gameodds(home_odds=1.8, away_odds=2.0)

        self.strategy.evaluate(event, odds_initial, [], 1000, 0)  # init state

        opportunities = self.strategy.evaluate_batch(
            us_events=[event],
            odds_by_game={event.slug: odds_slight_edge},
            existing_opportunities=[],
            bankroll=1000,
            open_exposure=0,
        )
        self.assertEqual(
            len(opportunities), 0,
            "Should return no opportunities if edge is below min_edge",
        )

    # ------------------------------------------------------------------
    # Successful signal
    # ------------------------------------------------------------------

    def test_successful_delay_arb_signal(self):
        """Book shifts big while US price is stale => opportunity created."""
        # US price stays at 50c throughout
        event = create_mock_usevent(yes_price=0.50)

        # Initial odds: 50/50 → home_prob ≈ 0.50
        odds_initial = create_mock_gameodds(home_odds=2.0, away_odds=2.0)

        # Moved odds: home_odds=1.5, away_odds=2.0
        #   home_implied = 1/1.5 ≈ 0.6667, away_implied = 0.5, total = 1.1667
        #   home_prob ≈ 0.5714  →  delta ≈ 0.071 > 0.03 ✓  edge ≈ 0.071 > 0.04 ✓
        odds_big_move = create_mock_gameodds(home_odds=1.5, away_odds=2.0)

        # First call: init state
        self.strategy.evaluate(event, odds_initial, [], 1000, 0)

        # Second call: big movement, stale US price
        opportunities = self.strategy.evaluate_batch(
            us_events=[event],
            odds_by_game={event.slug: odds_big_move},
            existing_opportunities=[],
            bankroll=1000,
            open_exposure=0,
        )
        self.assertEqual(len(opportunities), 1, "Should find one delay arb opportunity")
        opp = opportunities[0]
        self.assertGreater(opp.edge, 0.04, "Edge should exceed min_edge threshold")
        self.assertIn("delay_arb", opp.id, "Opportunity ID should identify the strategy")

    # ------------------------------------------------------------------
    # Cooldown
    # ------------------------------------------------------------------

    def test_cooldown_prevents_double_signal(self):
        """After a signal fires, cooldown blocks the next evaluation."""
        event = create_mock_usevent(yes_price=0.50)
        odds_initial = create_mock_gameodds(home_odds=2.0, away_odds=2.0)
        odds_big_move = create_mock_gameodds(home_odds=1.5, away_odds=2.0)

        # Trigger signal once
        self.strategy.evaluate(event, odds_initial, [], 1000, 0)
        first = self.strategy.evaluate_batch(
            us_events=[event],
            odds_by_game={event.slug: odds_big_move},
            existing_opportunities=[],
            bankroll=1000,
            open_exposure=0,
        )
        self.assertEqual(len(first), 1, "First pass should produce a signal")

        # Immediate re-evaluation should be blocked by cooldown
        second = self.strategy.evaluate_batch(
            us_events=[event],
            odds_by_game={event.slug: odds_big_move},
            existing_opportunities=[],
            bankroll=1000,
            open_exposure=0,
        )
        self.assertEqual(len(second), 0, "Should be blocked by cooldown")

    # ------------------------------------------------------------------
    # Existing opportunities dedup
    # ------------------------------------------------------------------

    def test_skips_existing_opportunity(self):
        """If the slug already appears in existing_opportunities, skip it."""
        event = create_mock_usevent(slug="dedup-slug", yes_price=0.50)
        odds_initial = create_mock_gameodds(home_odds=2.0, away_odds=2.0)
        odds_big_move = create_mock_gameodds(home_odds=1.5, away_odds=2.0)

        self.strategy.evaluate(event, odds_initial, [], 1000, 0)

        opportunities = self.strategy.evaluate_batch(
            us_events=[event],
            odds_by_game={event.slug: odds_big_move},
            existing_opportunities=["dedup-slug"],  # already open
            bankroll=1000,
            open_exposure=0,
        )
        self.assertEqual(len(opportunities), 0, "Should skip slug already in existing opportunities")


if __name__ == "__main__":
    unittest.main()
