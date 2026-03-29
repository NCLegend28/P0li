"""
ESPN unofficial API client — schedule and injury data.

No auth required. Free, no rate limit enforced (be polite — cache results).
Used to enrich sports strategy signals: B2B schedules, injury reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date

import httpx
from loguru import logger

_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# ESPN sport/league path fragments
_LEAGUE_PATHS = {
    "NBA": "basketball/nba",
    "NFL": "football/nfl",
    "MLB": "baseball/mlb",
    "NHL": "hockey/nhl",
    "UFC": "mma/ufc",
    "EPL": "soccer/eng.1",
    "UCL": "soccer/uefa.champions",
}


@dataclass
class Game:
    """A scheduled game from ESPN."""
    league: str
    home_team: str
    away_team: str
    game_id: str
    commence_time: datetime
    status: str   # "scheduled", "in_progress", "final"


@dataclass
class InjuryReport:
    """Player injury status from ESPN."""
    team: str
    player: str
    position: str
    status: str       # "Questionable", "Out", "Doubtful", "Probable"
    description: str
    updated_at: datetime = field(default_factory=lambda: datetime.utcnow())


class ESPNClient:
    """
    ESPN unofficial API client.

    Provides schedule data (for B2B detection) and injury reports
    (for last-minute probability adjustments before game time).
    """

    def __init__(self, timeout: float = 10.0):
        self._timeout = timeout

    async def fetch_schedule(self, league: str, for_date: date | None = None) -> list[Game]:
        """
        Fetch today's (or a given date's) schedule for a league.

        B2B detection: call this for yesterday AND today, compare teams.
        """
        path = _LEAGUE_PATHS.get(league.upper(), league.lower())
        date_str = (for_date or date.today()).strftime("%Y%m%d")
        url = f"{_BASE}/{path}/scoreboard"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(url, params={"dates": date_str})
                resp.raise_for_status()
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                logger.warning("ESPN schedule fetch failed for {} {}: {}", league, date_str, e)
                return []

        games: list[Game] = []
        for event in resp.json().get("events", []):
            competition = event.get("competitions", [{}])[0]
            competitors = competition.get("competitors", [])
            if len(competitors) < 2:
                continue

            home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
            away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
            home_name = home.get("team", {}).get("displayName", "")
            away_name = away.get("team", {}).get("displayName", "")

            status_type = event.get("status", {}).get("type", {}).get("name", "scheduled").lower()
            commence_raw = event.get("date", "")
            try:
                commence = datetime.fromisoformat(commence_raw.replace("Z", "+00:00"))
            except ValueError:
                commence = datetime.utcnow()

            games.append(Game(
                league=league,
                home_team=home_name,
                away_team=away_name,
                game_id=event.get("id", ""),
                commence_time=commence,
                status=status_type,
            ))

        logger.debug("ESPN: {} games for {} on {}", len(games), league, date_str)
        return games

    async def fetch_injuries(self, league: str) -> list[InjuryReport]:
        """
        Fetch current injury report for a league.

        Returns players listed as Questionable, Doubtful, or Out.
        """
        path = _LEAGUE_PATHS.get(league.upper(), league.lower())
        url = f"{_BASE}/{path}/injuries"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                logger.warning("ESPN injuries fetch failed for {}: {}", league, e)
                return []

        injuries: list[InjuryReport] = []
        for team_entry in resp.json().get("injuries", []):
            team_name = team_entry.get("team", {}).get("displayName", "")
            for inj in team_entry.get("injuries", []):
                athlete = inj.get("athlete", {})
                status = inj.get("status", "")
                if status.lower() in ("active", "day-to-day"):
                    continue   # skip non-impactful statuses
                injuries.append(InjuryReport(
                    team=team_name,
                    player=athlete.get("displayName", ""),
                    position=athlete.get("position", {}).get("abbreviation", ""),
                    status=status,
                    description=inj.get("longComment", inj.get("shortComment", "")),
                ))

        logger.debug("ESPN: {} injuries for {}", len(injuries), league)
        return injuries

    def is_back_to_back(
        self,
        team: str,
        yesterday_games: list[Game],
        today_games: list[Game],
    ) -> bool:
        """
        Return True if `team` played yesterday AND plays today.
        Relevant for NBA where B2B fatigue is well-documented.
        """
        team_lower = team.lower()
        played_yesterday = any(
            team_lower in g.home_team.lower() or team_lower in g.away_team.lower()
            for g in yesterday_games
        )
        plays_today = any(
            team_lower in g.home_team.lower() or team_lower in g.away_team.lower()
            for g in today_games
        )
        return played_yesterday and plays_today
