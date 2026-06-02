"""
The Odds API client — Layer 2 sports signal (sportsbook consensus).

Used as CONFIRMATION only, not as the primary signal.
500 req/month on the free tier — fine for spot-checking, not continuous scanning.

Ref: https://the-odds-api.com/liveapi/guides/v4/
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx
from loguru import logger

_BASE_URL = "https://api.the-odds-api.com/v4"

# Sport keys used by The Odds API
SPORT_KEYS = {
    "NBA": "basketball_nba",
    "NFL": "americanfootball_nfl",
    "MLB": "baseball_mlb",
    "NHL": "icehockey_nhl",
    "UFC": "mma_mixed_martial_arts",
    "EPL": "soccer_epl",
    "UCL": "soccer_uefa_champs_league",
}


@dataclass
class GameOdds:
    """De-vigged sportsbook consensus for a single game."""
    sport: str
    home_team: str
    away_team: str
    home_prob: float    # true probability (de-vigged)
    away_prob: float    # true probability (de-vigged)
    bookmakers_count: int
    commence_time: datetime


@dataclass
class GameScore:
    """Live or final score for a game."""
    sport: str
    home_team: str
    away_team: str
    home_score: int | None
    away_score: int | None
    completed: bool
    commence_time: datetime


def _devig(home_implied: float, away_implied: float) -> tuple[float, float]:
    """
    Remove bookmaker vig from raw implied probabilities.
    Raw probs sum to ~1.05 (5% vig); true probs sum to 1.0.
    """
    total = home_implied + away_implied
    if total <= 0:
        return 0.5, 0.5
    return round(home_implied / total, 4), round(away_implied / total, 4)


class OddsClient:
    """
    The Odds API — secondary confirmation signal for the sports strategy.

    Fetches consensus odds from 15+ bookmakers, de-vigs them, and returns
    clean win probabilities for use in compute_confirmed_edge().
    """

    def __init__(self, api_key: str, timeout: float = 15.0):
        self._api_key = api_key
        self._timeout = timeout

    async def fetch_odds(
        self,
        sport: str,             # e.g. "NBA", "NFL", or raw key like "basketball_nba"
        region: str = "us",
        markets: str = "h2h",
    ) -> list[GameOdds]:
        """
        Fetch moneyline odds for all upcoming games in a sport.

        Returns de-vigged probabilities. Logs remaining quota from response headers.
        """
        sport_key = SPORT_KEYS.get(sport.upper(), sport)
        params = {
            "apiKey": self._api_key,
            "regions": region,
            "markets": markets,
            "oddsFormat": "decimal",
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(
                    f"{_BASE_URL}/sports/{sport_key}/odds",
                    params=params,
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error("Odds API HTTP error for {}: {}", sport, e)
                return []
            except httpx.RequestError as e:
                logger.error("Odds API request error for {}: {}", sport, e)
                return []

        # Log remaining quota
        remaining = resp.headers.get("x-requests-remaining", "?")
        used = resp.headers.get("x-requests-used", "?")
        logger.debug("Odds API quota: {} used, {} remaining", used, remaining)

        games = resp.json()
        results: list[GameOdds] = []

        for game in games:
            home = game.get("home_team", "")
            away = game.get("away_team", "")
            commence = datetime.fromisoformat(game.get("commence_time", "").replace("Z", "+00:00"))

            home_probs: list[float] = []
            away_probs: list[float] = []

            for bookmaker in game.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    if market.get("key") != "h2h":
                        continue
                    outcomes = {o["name"]: o["price"] for o in market.get("outcomes", [])}
                    home_dec = outcomes.get(home, 0)
                    away_dec = outcomes.get(away, 0)
                    if home_dec > 1 and away_dec > 1:
                        # Convert decimal odds to implied probability
                        home_probs.append(1 / home_dec)
                        away_probs.append(1 / away_dec)

            if not home_probs:
                continue

            avg_home = sum(home_probs) / len(home_probs)
            avg_away = sum(away_probs) / len(away_probs)
            home_true, away_true = _devig(avg_home, avg_away)

            results.append(GameOdds(
                sport=sport,
                home_team=home,
                away_team=away,
                home_prob=home_true,
                away_prob=away_true,
                bookmakers_count=len(home_probs),
                commence_time=commence,
            ))

        logger.info("Odds API: {} games fetched for {}", len(results), sport)
        return results

    async def fetch_scores(self, sport: str) -> list[GameScore]:
        """Fetch live/recent scores for a sport."""
        sport_key = SPORT_KEYS.get(sport.upper(), sport)
        params = {"apiKey": self._api_key, "daysFrom": 1}

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(
                    f"{_BASE_URL}/sports/{sport_key}/scores",
                    params=params,
                )
                resp.raise_for_status()
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                logger.error("Odds API scores error for {}: {}", sport, e)
                return []

        results: list[GameScore] = []
        for game in resp.json():
            scores_raw = {s["name"]: s.get("score") for s in game.get("scores") or []}
            home = game.get("home_team", "")
            away = game.get("away_team", "")
            results.append(GameScore(
                sport=sport,
                home_team=home,
                away_team=away,
                home_score=int(scores_raw[home]) if scores_raw.get(home) is not None else None,
                away_score=int(scores_raw[away]) if scores_raw.get(away) is not None else None,
                completed=game.get("completed", False),
                commence_time=datetime.fromisoformat(
                    game.get("commence_time", "").replace("Z", "+00:00")
                ),
            ))

        return results
