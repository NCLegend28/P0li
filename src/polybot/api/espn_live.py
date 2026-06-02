"""
ESPN live game state client.

Fetches in-progress scores, period, and clock from the ESPN unofficial API.
The same scoreboard endpoint used by ESPNClient already returns this data —
this module parses the live fields that espn.py intentionally ignores.

No auth required. Results are cached per game_id for `poll_interval` seconds
to avoid hammering the ESPN API on every 30-second scan.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from loguru import logger

from polybot.models import LiveGameContext

_BASE = "https://site.api.espn.com/apis/site/v2/sports"

_LEAGUE_PATHS = {
    "NBA": "basketball/nba",
    "NFL": "football/nfl",
    "MLB": "baseball/mlb",
    "NHL": "hockey/nhl",
    "EPL": "soccer/eng.1",
    "UCL": "soccer/uefa.champions",
    "MLS": "soccer/usa.1",
}

# Total regulation seconds per sport (used to compute seconds_remaining)
_REGULATION_SECONDS: dict[str, float] = {
    "NBA": 48 * 60,       # 4 x 12 min
    "NFL": 60 * 60,       # 4 x 15 min
    "MLB": 0.0,           # innings-based — not time-dependent
    "NHL": 60 * 60,       # 3 x 20 min
    "EPL": 90 * 60,
    "UCL": 90 * 60,
    "MLS": 90 * 60,
}

# Seconds per period per sport
_PERIOD_SECONDS: dict[str, float] = {
    "NBA": 12 * 60,
    "NFL": 15 * 60,
    "NHL": 20 * 60,
    "EPL": 45 * 60,
    "UCL": 45 * 60,
    "MLS": 45 * 60,
}


def _parse_clock(clock_str: str) -> float:
    """
    Convert an ESPN clock string to seconds.

    Handles formats:
      "4:32"   → 272.0
      "32"     → 32.0  (seconds only, e.g. final minute display)
      ""       → 0.0
    """
    clock_str = clock_str.strip()
    if not clock_str:
        return 0.0
    parts = clock_str.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return 0.0


def seconds_remaining_in_game(
    period: int,
    clock_seconds: float,
    sport: str,
    is_overtime: bool = False,
) -> float:
    """
    Compute total seconds remaining in the game.

    For quarter/period sports (NBA, NFL, NHL): remaining = clock + periods_left * period_duration
    For soccer: remaining = (90 - current_minute) * 60, clamped to 0
    For MLB: returns 0.0 (innings-based, not time-modelled)

    OT periods use their own duration (NBA OT = 5 min, NFL OT = 10 min).
    """
    sport = sport.upper()

    if sport == "MLB":
        return 0.0

    period_secs = _PERIOD_SECONDS.get(sport, 0.0)
    reg_secs    = _REGULATION_SECONDS.get(sport, 0.0)

    if sport in ("EPL", "UCL", "MLS"):
        # For soccer ESPN reports elapsed minutes in `clock` field, not remaining.
        # clock_seconds here = elapsed seconds; convert to remaining.
        elapsed = clock_seconds
        return max(0.0, reg_secs - elapsed)

    if is_overtime:
        # OT: just return the clock value — don't add future periods
        return max(0.0, clock_seconds)

    # Quarter/period sports
    total_periods = int(reg_secs / period_secs) if period_secs else 4
    periods_remaining_after_this = max(0, total_periods - period)
    return max(0.0, clock_seconds + periods_remaining_after_this * period_secs)


class ESPNLiveClient:
    """
    Fetches live game state (score, period, clock) from ESPN.

    Results are cached per game_id for `poll_interval` seconds so repeated
    calls within the same scan cycle don't make redundant HTTP requests.
    """

    def __init__(self, poll_interval: int = 30, timeout: float = 10.0):
        self._poll_interval = poll_interval
        self._timeout = timeout
        # Cache: game_id → (LiveGameContext, fetched_timestamp)
        self._cache: dict[str, tuple[LiveGameContext, float]] = {}

    def _cached(self, game_id: str) -> LiveGameContext | None:
        entry = self._cache.get(game_id)
        if entry and (time.monotonic() - entry[1]) < self._poll_interval:
            return entry[0]
        return None

    def _store(self, ctx: LiveGameContext) -> None:
        self._cache[ctx.game_id] = (ctx, time.monotonic())

    def _parse_event(self, event: dict, sport: str) -> LiveGameContext | None:
        """Parse a single ESPN scoreboard event dict into a LiveGameContext."""
        game_id  = event.get("id", "")
        status   = event.get("status", {})
        type_obj = status.get("type", {})
        state    = type_obj.get("name", "").lower()   # "scheduled", "in_progress", "final"

        if state not in ("in_progress", "final"):
            return None

        competition = (event.get("competitions") or [{}])[0]
        competitors = competition.get("competitors", [])
        if len(competitors) < 2:
            return None

        home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
        away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])

        try:
            home_score = int(home.get("score", 0))
            away_score = int(away.get("score", 0))
        except (TypeError, ValueError):
            home_score = away_score = 0

        # Period / clock
        period       = status.get("period", 1)
        clock_str    = status.get("displayClock", "")
        clock_secs   = _parse_clock(clock_str)
        is_overtime  = type_obj.get("shortDetail", "").upper().startswith("OT")
        secs_left    = seconds_remaining_in_game(period, clock_secs, sport, is_overtime)

        return LiveGameContext(
            game_id=game_id,
            sport=sport.upper(),
            home_team=home.get("team", {}).get("displayName", ""),
            away_team=away.get("team", {}).get("displayName", ""),
            home_score=home_score,
            away_score=away_score,
            period=period,
            seconds_remaining=secs_left,
            is_final=(state == "final"),
            fetched_at=datetime.now(timezone.utc),
        )

    async def fetch_all_live(self, league: str) -> list[LiveGameContext]:
        """
        Fetch all in-progress (and recently final) games for a league.

        Returns only games that are live or just finished — skips scheduled games.
        Uses the cache; only fetches from ESPN when the TTL has expired.
        """
        path = _LEAGUE_PATHS.get(league.upper(), league.lower())
        url  = f"{_BASE}/{path}/scoreboard"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                logger.warning("ESPNLive: scoreboard fetch failed for {}: {}", league, e)
                return []

        results: list[LiveGameContext] = []
        for event in resp.json().get("events", []):
            cached = self._cached(event.get("id", ""))
            if cached:
                results.append(cached)
                continue

            ctx = self._parse_event(event, league)
            if ctx:
                self._store(ctx)
                results.append(ctx)

        logger.debug("ESPNLive: {} live/final games for {}", len(results), league)
        return results

    async def fetch_live_state(self, game_id: str, league: str) -> LiveGameContext | None:
        """
        Fetch live state for a specific game by ESPN game ID.

        Uses cache first; falls back to the summary endpoint.
        """
        cached = self._cached(game_id)
        if cached:
            return cached

        path = _LEAGUE_PATHS.get(league.upper(), league.lower())
        url  = f"{_BASE}/{path}/summary"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(url, params={"event": game_id})
                resp.raise_for_status()
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                logger.warning("ESPNLive: summary fetch failed for game {}: {}", game_id, e)
                return None

        data  = resp.json()
        # The summary endpoint wraps the event under "header.competitions"
        header       = data.get("header", {})
        competitions = header.get("competitions", [{}])
        if not competitions:
            return None

        # Reconstruct a minimal event-like dict the parser expects
        comp   = competitions[0]
        status = comp.get("status", {})
        event_like = {
            "id": game_id,
            "status": status,
            "competitions": [comp],
        }
        ctx = self._parse_event(event_like, league)
        if ctx:
            self._store(ctx)
        return ctx
