"""
Gamma API client — Polymarket's market metadata layer.

Base URL: https://gamma-api.polymarket.com
Docs:     https://docs.polymarket.com/developers/gamma-markets-api/markets
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger

from polybot.models import Market, MarketCategory, Outcome
from polybot.utils.retry import async_retry

GAMMA_BASE  = "https://gamma-api.polymarket.com"
CLOB_BASE   = "https://clob.polymarket.com"

# Gamma's tag API is unreliable — detect by question text instead
_WEATHER_KEYWORDS = (
    "temperature", "°f", "°c", "degrees", "fahrenheit",
    "celsius", "highest temp", "lowest temp",
)
_CRYPTO_KEYWORDS  = ("bitcoin", "btc", "ethereum", "eth", "crypto", "sol ", "xrp")
_POLITICS_KEYWORDS = ("election", "president", "senate", "congress", "vote", "trump", "biden")

# League acronyms + common team/sport terms.
# Team-name-only questions ("Will the Lakers beat…") lack acronyms but
# usually include the team name or venue in one of these lists.
_SPORTS_KEYWORDS  = (
    # League acronyms
    "nba", "nfl", "mlb", "nhl", "mls", "epl", "ufc", "wnba",
    "premier league", "champions league", "la liga", "serie a",
    "bundesliga", "ligue 1", "ncaa", "march madness",
    # Game types
    " game ", " match ", " series ", "playoff", "championship",
    "super bowl", "world series", "stanley cup", "nba finals",
    "world cup", "euro ", " cup ",
    # NBA teams (enough to catch most matchup questions)
    "lakers", "celtics", "warriors", "bulls", "heat", "nets",
    "knicks", "bucks", "sixers", "suns", "nuggets", "clippers",
    "raptors", "mavs", "mavericks", "rockets", "spurs", "thunder",
    "blazers", "jazz", "kings", "pistons", "cavs", "cavaliers",
    "hawks", "hornets", "magic", "pacers", "grizzlies", "pelicans",
    "wizards", "timberwolves", "wolves",
    # NFL teams
    "patriots", "chiefs", "cowboys", "packers", "eagles", "steelers",
    "ravens", "niners", "49ers", "broncos", "seahawks", "rams",
    "chargers", "raiders", "dolphins", "bills", "colts", "texans",
    "jaguars", "titans", "bengals", "browns", "giants", "commanders",
    "falcons", "panthers", "saints", "buccaneers", "cardinals", "bears",
    "lions", "vikings",
    # MLB teams
    "yankees", "red sox", "dodgers", "cubs", "mets", "braves",
    "astros", "padres", "phillies", "cardinals", "giants", "athletics",
    # NHL teams
    "maple leafs", "canadiens", "bruins", "rangers", "blackhawks",
    "penguins", "flyers", "capitals", "avalanche", "oilers", "canucks",
    # Soccer / other
    "liverpool", "manchester", "arsenal", "chelsea", "barcelona",
    "real madrid", "bayern", "psg", "juventus",
    # Generic sports verbs that rarely appear outside sports questions
    " win the ", "beat the ", "cover the spread",
)


_SPORTS_TAG_SLUGS = {
    "sports", "nba", "nfl", "mlb", "nhl", "soccer", "tennis", "golf",
    "mma", "ufc", "boxing", "formula-1", "f1", "esports", "cricket",
    "rugby", "olympics", "ncaa",
}


def _parse_category(tags: list[dict], question: str = "") -> MarketCategory:
    q = question.lower()
    if any(kw in q for kw in _WEATHER_KEYWORDS):
        return MarketCategory.WEATHER
    if any(kw in q for kw in _CRYPTO_KEYWORDS):
        return MarketCategory.CRYPTO
    if any(kw in q for kw in _POLITICS_KEYWORDS):
        return MarketCategory.POLITICS
    if any(kw in q for kw in _SPORTS_KEYWORDS):
        return MarketCategory.SPORTS
    # Fall back to tag slugs if question text didn't match
    for tag in tags:
        slug = tag.get("slug", tag.get("label", "")).lower()
        if slug in _SPORTS_TAG_SLUGS:
            return MarketCategory.SPORTS
    return MarketCategory.OTHER


def _parse_outcomes(raw: dict) -> list[Outcome]:
    """
    Gamma returns outcomes as parallel JSON arrays:
      outcomes:       '["Yes","No"]'
      outcomePrices:  '["0.62","0.38"]'
      clobTokenIds:   '["token_a","token_b"]'
    """
    import json

    names  = json.loads(raw.get("outcomes", '["Yes","No"]'))
    prices = json.loads(raw.get("outcomePrices", '["0.5","0.5"]'))
    tokens = json.loads(raw.get("clobTokenIds", '["",""]'))

    return [
        Outcome(
            name        = names[i],
            price       = float(prices[i]),
            clobTokenId = tokens[i] if i < len(tokens) else "",
        )
        for i in range(len(names))
    ]


def _parse_market(raw: dict) -> Market | None:
    end_date_str = raw.get("endDate") or raw.get("endDateIso")
    if not end_date_str:
        return None

    end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))

    return Market(
        id            = str(raw["id"]),
        question      = raw.get("question", ""),
        category      = _parse_category(raw.get("tags", []), raw.get("question", "")),
        end_date      = end_date,
        liquidity_usd = float(raw.get("liquidity", 0)),
        volume_usd    = float(raw.get("volume", 0)),
        outcomes      = _parse_outcomes(raw),
        active        = raw.get("active", True),
        closed        = raw.get("closed", False),
    )


class GammaClient:
    def __init__(self, timeout: float = 15.0):
        self._client = httpx.AsyncClient(
            base_url = GAMMA_BASE,
            timeout  = timeout,
            headers  = {"Accept": "application/json"},
        )

    async def __aenter__(self) -> GammaClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._client.aclose()

    @async_retry(max_attempts=3, base_delay=2.0, exceptions=(httpx.HTTPError, httpx.TimeoutException))
    async def fetch_markets(
        self,
        *,
        limit:           int   = 100,
        min_liquidity:   float = 500.0,
        category:        MarketCategory | None = None,
        active_only:     bool  = True,
    ) -> list[Market]:
        """
        Fetch open markets from Gamma, filter by liquidity.
        Gamma paginates in chunks of 100; we fetch until we have enough.
        """
        params: dict[str, Any] = {
            "limit":   limit,
            "active":  "true" if active_only else "false",
            "closed":  "false",
            "order":   "volume",
            "ascending": "false",
        }

        if category:
            params["tag"] = category.value

        logger.debug(f"Gamma fetch → {params}")
        response = await self._client.get("/markets", params=params)
        response.raise_for_status()

        raw_markets: list[dict] = response.json()
        markets: list[Market] = []

        for raw in raw_markets:
            market = _parse_market(raw)
            if market is None:
                continue
            if market.closed or not market.active:
                continue
            if market.liquidity_usd < min_liquidity:
                continue
            if market.hours_until_close < 1.0:
                # Skip markets closing in less than 1 hour
                continue
            markets.append(market)

        logger.info(f"Gamma returned {len(raw_markets)} raw, {len(markets)} passed filters")
        return markets

    async def fetch_weather_markets(self, min_liquidity: float = 200.0) -> list[Market]:
        """Shortcut: fetch only weather markets with a lower liquidity bar."""
        return await self.fetch_markets(
            limit         = 200,
            min_liquidity = min_liquidity,
            category      = MarketCategory.WEATHER,
        )

    @async_retry(max_attempts=3, base_delay=2.0, exceptions=(httpx.HTTPError, httpx.TimeoutException))
    async def fetch_market_by_id(self, market_id: str) -> Market:
        response = await self._client.get(f"/markets/{market_id}")
        response.raise_for_status()
        return _parse_market(response.json())
