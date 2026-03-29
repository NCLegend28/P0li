"""
Sports scanner graph — LangGraph pipeline.

Full node pipeline:
  fetch_global_sports
    → fetch_us_events
      → match_markets
        → fetch_odds_and_schedule
          → run_sports_strategy
            → monitor_sports_positions
              → END

Layer assignments:
  fetch_global_sports     → Gamma API (Layer 1, READ ONLY — no auth, shared with weather bot)
  fetch_us_events         → Polymarket US SDK (Layer 3, requires Ed25519 auth)
  match_markets           → internal fuzzy matching
  fetch_odds_and_schedule → The Odds API (Layer 2) + ESPN (schedule/injuries)
  run_sports_strategy     → sports.py strategy engine
  monitor_sports_positions → exit.py (pregame_lock + standard triggers)

Import firewall:
  This file imports gamma.py (Layer 1 reads) and uses AsyncPolymarketUSClient
  (Layer 3 reads). These are separate clients — no orders are ever placed here.
  Order execution happens in paper/trader.py via PolymarketUSClient (sync).
"""

from __future__ import annotations

import asyncio
import difflib
from datetime import date, timedelta
from typing import Any

from langgraph.graph import StateGraph, END
from loguru import logger

from polybot.api.espn import ESPNClient
from polybot.api.gamma import GammaClient
from polybot.api.odds import OddsClient, SPORT_KEYS
from polybot.config import settings
from polybot.models import Market, MarketCategory
from polybot.scanner.sports_state import MatchedPair, SportsScanState
from polybot.strategies.exit import compute_exit_signals
from polybot.strategies.sports import evaluate_sports_markets

# Sports leagues to scan on the global Gamma API
_SPORTS_KEYWORDS = ["NBA", "NFL", "MLB", "NHL", "FIFA", "UFC",
                    "Premier League", "Champions League", "MLS", "WNBA"]

# Min match score to consider a global ↔ US pair as the same game
_MIN_MATCH_SCORE = 0.45


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fuzzy_score(a: str, b: str) -> float:
    """SequenceMatcher ratio between two lowercased strings."""
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _best_match(
    global_question: str,
    us_markets: list[dict],
) -> tuple[dict | None, float]:
    """
    Find the best matching US market for a global Gamma market question.

    Matches on title/slug similarity and shared team-name tokens.
    Returns (best_market_dict, score).
    """
    if not us_markets:
        return None, 0.0

    best: dict | None = None
    best_score = 0.0

    q_lower = global_question.lower()
    q_tokens = set(q_lower.split())

    for us_mkt in us_markets:
        title = us_mkt.get("title", "") or us_mkt.get("name", "") or ""
        slug  = us_mkt.get("slug", "")

        # Token overlap score
        t_tokens = set(title.lower().split())
        overlap = len(q_tokens & t_tokens)
        token_score = overlap / max(len(q_tokens), 1)

        # String similarity score
        sim_title = _fuzzy_score(global_question, title)
        sim_slug  = _fuzzy_score(global_question, slug.replace("-", " "))

        score = max(token_score, sim_title, sim_slug)

        if score > best_score:
            best_score = score
            best = us_mkt

    return best, best_score


def _extract_us_yes_price(us_market: dict) -> float:
    """Extract the YES price from a US market dict."""
    # Try common field names returned by the US SDK
    for field in ("yesPrice", "yes_price", "price"):
        val = us_market.get(field)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass

    # Try outcomes array
    for outcome in us_market.get("outcomes", []):
        if outcome.get("name", "").upper() == "YES":
            try:
                return float(outcome.get("price", 0.5))
            except (TypeError, ValueError):
                pass

    return 0.5   # fallback


def _estimate_book_depth(us_market: dict) -> float:
    """Estimate USD depth from the US market dict."""
    for field in ("liquidity", "depth", "liquidityUsd", "volume"):
        val = us_market.get(field)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return 0.0


# ─── Node: fetch_global_sports ────────────────────────────────────────────────

async def fetch_global_sports(state: SportsScanState) -> dict[str, Any]:
    """
    Layer 1: Fetch sports markets from the global Gamma API.

    Reuses the same gamma.py client as the weather bot.
    This is READ-ONLY — no auth, no orders, just price data.
    """
    logger.info("SPORTS Layer 1: Fetching global sports from Gamma API...")
    async with GammaClient() as gamma:
        markets = await gamma.fetch_markets(
            limit=500,
            min_liquidity=200.0,   # lower threshold for sports (thinner than weather)
        )

    sports_markets = [
        m for m in markets
        if m.category == MarketCategory.SPORTS
        and m.hours_until_close >= 1.0
        and 0.05 <= m.yes_price <= 0.95
    ]

    logger.info(
        "SPORTS Layer 1: {}/{} global markets qualify (sports, active, priced)",
        len(sports_markets), len(markets),
    )
    return {"global_sports": sports_markets}


# ─── Node: fetch_us_events ────────────────────────────────────────────────────

async def fetch_us_events(state: SportsScanState) -> dict[str, Any]:
    """
    Layer 3: Fetch active events from the Polymarket US SDK.

    Requires POLYMARKET_KEY_ID + POLYMARKET_SECRET_KEY.
    If keys are not configured, returns empty list (sports bot degrades gracefully).
    """
    if not settings.polymarket_key_id or not settings.polymarket_secret_key:
        logger.warning(
            "SPORTS Layer 3: POLYMARKET_KEY_ID/SECRET not set — "
            "add them to .env to enable US market scanning"
        )
        return {"us_events": []}

    from polybot.api.polymarket_us import AsyncPolymarketUSClient

    logger.info("SPORTS Layer 3: Fetching US events from Polymarket US SDK...")
    client = AsyncPolymarketUSClient(
        key_id=settings.polymarket_key_id,
        secret_key=settings.polymarket_secret_key,
    )
    try:
        result = await client.list_events(limit=100, active=True)
        events = result.get("events", []) if isinstance(result, dict) else result
        logger.info("SPORTS Layer 3: {} active US events", len(events))

        # Flatten events → list of market dicts with slug/title/price
        markets: list[dict] = []
        for event in events:
            for mkt in event.get("markets", [event]):
                markets.append(mkt)

        return {"us_events": markets}
    except Exception as e:
        logger.error("SPORTS Layer 3: US SDK fetch failed: {}", e)
        return {"us_events": []}
    finally:
        await client.close()


# ─── Node: match_markets ──────────────────────────────────────────────────────

async def match_markets(state: SportsScanState) -> dict[str, Any]:
    """
    Match global Gamma markets to their Polymarket US equivalents.

    Uses fuzzy string matching on market titles/slugs.
    Only pairs with score >= _MIN_MATCH_SCORE are kept.
    """
    if not state.global_sports:
        logger.info("SPORTS: No global sports markets to match")
        return {"matched_pairs": []}

    if not state.us_events:
        logger.warning(
            "SPORTS: No US events available — "
            "cannot compute cross-platform edge without Layer 3 data"
        )
        return {"matched_pairs": []}

    pairs: list[MatchedPair] = []

    for global_mkt in state.global_sports:
        us_mkt, score = _best_match(global_mkt.question, state.us_events)
        if us_mkt is None or score < _MIN_MATCH_SCORE:
            continue

        us_yes_price = _extract_us_yes_price(us_mkt)
        us_book_depth = _estimate_book_depth(us_mkt)
        us_slug  = us_mkt.get("slug", us_mkt.get("id", ""))
        us_title = us_mkt.get("title", us_mkt.get("name", ""))

        pairs.append(MatchedPair(
            global_market=global_mkt,
            us_slug=us_slug,
            us_title=us_title,
            us_yes_price=us_yes_price,
            us_book_depth=us_book_depth,
            match_score=score,
        ))

    pairs.sort(key=lambda p: abs(p.global_market.yes_price - p.us_yes_price), reverse=True)

    logger.info(
        "SPORTS: Matched {}/{} global markets to US equivalents",
        len(pairs), len(state.global_sports),
    )
    if pairs:
        top = pairs[0]
        logger.debug(
            "SPORTS: Largest gap → {} global={:.3f} us={:.3f} diff={:.3f}",
            top.us_slug,
            top.global_market.yes_price,
            top.us_yes_price,
            abs(top.global_market.yes_price - top.us_yes_price),
        )

    return {"matched_pairs": pairs}


# ─── Node: fetch_odds_and_schedule ────────────────────────────────────────────

async def fetch_odds_and_schedule(state: SportsScanState) -> dict[str, Any]:
    """
    Layer 2 + Schedule:
      - The Odds API: sportsbook confirmation (500 req/month free — used sparingly)
      - ESPN: today/yesterday schedules (B2B detection) + injury reports

    Both are optional. If keys/APIs are unavailable, strategy still runs
    with lower confidence scores (0.7 instead of 1.0).
    """
    if not state.matched_pairs:
        return {"odds_data": [], "injuries": [], "today_games": [], "yesterday_games": []}

    odds_list = []
    injuries = []
    today_games: list = []
    yesterday_games: list = []

    # ── The Odds API (Layer 2) ────────────────────────────────────────────────
    if settings.odds_api_key:
        odds_client = OddsClient(api_key=settings.odds_api_key)
        active_sports = {
            keyword
            for pair in state.matched_pairs
            for keyword in _SPORTS_KEYWORDS
            if keyword.upper() in pair.global_market.question.upper()
            and keyword.upper() in SPORT_KEYS
        }

        for sport in list(active_sports)[:3]:   # cap at 3 sports to preserve quota
            try:
                game_odds = await odds_client.fetch_odds(sport)
                odds_list.extend(game_odds)
                logger.info("SPORTS Layer 2: {} {} games from Odds API", len(game_odds), sport)
            except Exception as e:
                logger.warning("SPORTS Layer 2: Odds API failed for {}: {}", sport, e)
    else:
        logger.debug("SPORTS Layer 2: ODDS_API_KEY not set — using Layer 1 alone (conf=0.7)")

    # ── ESPN schedule + injuries ──────────────────────────────────────────────
    espn = ESPNClient()
    active_leagues = {
        keyword
        for pair in state.matched_pairs
        for keyword in ["NBA", "NFL", "MLB", "NHL"]
        if keyword in pair.global_market.question.upper()
    }

    async def _fetch_league(league: str):
        nonlocal today_games, yesterday_games
        try:
            td = await espn.fetch_schedule(league, for_date=date.today())
            yd = await espn.fetch_schedule(league, for_date=date.today() - timedelta(days=1))
            inj = await espn.fetch_injuries(league)
            return td, yd, inj
        except Exception as e:
            logger.warning("ESPN fetch failed for {}: {}", league, e)
            return [], [], []

    results = await asyncio.gather(*[_fetch_league(lg) for lg in active_leagues])
    for td, yd, inj in results:
        today_games.extend(td)
        yesterday_games.extend(yd)
        injuries.extend(inj)

    logger.info(
        "SPORTS support data: {} sportsbook games | {} injuries | {} today games",
        len(odds_list), len(injuries), len(today_games),
    )

    return {
        "odds_data": odds_list,
        "injuries": injuries,
        "today_games": today_games,
        "yesterday_games": yesterday_games,
    }


# ─── Node: run_sports_strategy ────────────────────────────────────────────────

async def run_sports_strategy(state: SportsScanState) -> dict[str, Any]:
    """
    Evaluate all matched pairs for cross-platform edge opportunities.
    """
    if not state.matched_pairs:
        logger.info("SPORTS strategy: no matched pairs to evaluate")
        return {"opportunities": []}

    # Build odds lookup keyed by us_slug
    odds_by_game: dict = {}
    for odds in state.odds_data:
        # Try to match odds to a US slug by team names
        for pair in state.matched_pairs:
            q = pair.global_market.question.lower()
            if (odds.home_team.lower() in q or odds.away_team.lower() in q):
                odds_by_game[pair.us_slug] = odds
                break

    matched_dicts = [
        {
            "global_market": p.global_market,
            "us_slug":        p.us_slug,
            "us_yes_price":   p.us_yes_price,
            "us_book_depth":  p.us_book_depth,
        }
        for p in state.matched_pairs
    ]

    opportunities = evaluate_sports_markets(
        matched_pairs=matched_dicts,
        odds_by_game=odds_by_game,
        injuries=state.injuries,
        today_games=state.today_games,
        yesterday_games=state.yesterday_games,
        min_edge=settings.sports_min_edge,
        position_size_usd=settings.paper_max_position_usd,
    )

    logger.info(
        "SPORTS strategy: {}/{} pairs → {} opportunities",
        len(state.matched_pairs), len(state.matched_pairs), len(opportunities),
    )

    return {"opportunities": opportunities}


# ─── Node: monitor_sports_positions ──────────────────────────────────────────

async def monitor_sports_positions(state: SportsScanState) -> dict[str, Any]:
    """
    Check open sports positions for exit conditions.

    Uses the standard exit engine (pregame_lock, time_stop, profit_target,
    edge_collapsed). Prices come from the latest matched pairs (US prices).
    """
    if not state.open_positions:
        return {"exit_signals": []}

    # Build price maps from matched pairs (US prices for sports positions)
    current_prices: dict[str, float] = {
        p.global_market.id: p.us_yes_price
        for p in state.matched_pairs
    }
    hours_to_close: dict[str, float] = {
        p.global_market.id: p.global_market.hours_until_close
        for p in state.matched_pairs
    }

    # For sports positions not in matched_pairs, fetch from global Gamma
    sports_positions = [
        t for t in state.open_positions
        if t.live_platform == "polymarket_us"
    ]
    missing_ids = [t.market_id for t in sports_positions if t.market_id not in current_prices]

    if missing_ids:
        logger.info("SPORTS: fetching {} stale position prices from Gamma", len(missing_ids))
        async with GammaClient() as gamma:
            for mid in missing_ids:
                try:
                    m = await gamma.fetch_market_by_id(mid)
                    if m:
                        current_prices[mid] = m.yes_price
                        hours_to_close[mid] = m.hours_until_close
                except Exception as e:
                    logger.warning("SPORTS: could not fetch market {}: {}", mid[:8], e)

    signals = compute_exit_signals(
        open_trades=sports_positions,
        current_prices=current_prices,
        hours_to_close=hours_to_close,
    )

    if signals:
        logger.info("SPORTS: {} exit signals generated", len(signals))

    return {"exit_signals": signals}


# ─── Graph assembly ───────────────────────────────────────────────────────────

def build_sports_scanner_graph() -> Any:
    """
    Assemble the sports scanner LangGraph pipeline.

    fetch_global_sports → fetch_us_events → match_markets
      → fetch_odds_and_schedule → run_sports_strategy
        → monitor_sports_positions → END
    """
    builder = StateGraph(SportsScanState)

    builder.add_node("fetch_global_sports",      fetch_global_sports)
    builder.add_node("fetch_us_events",          fetch_us_events)
    builder.add_node("match_markets",            match_markets)
    builder.add_node("fetch_odds_and_schedule",  fetch_odds_and_schedule)
    builder.add_node("run_sports_strategy",      run_sports_strategy)
    builder.add_node("monitor_sports_positions", monitor_sports_positions)

    builder.set_entry_point("fetch_global_sports")
    builder.add_edge("fetch_global_sports",      "fetch_us_events")
    builder.add_edge("fetch_us_events",          "match_markets")
    builder.add_edge("match_markets",            "fetch_odds_and_schedule")
    builder.add_edge("fetch_odds_and_schedule",  "run_sports_strategy")
    builder.add_edge("run_sports_strategy",      "monitor_sports_positions")
    builder.add_edge("monitor_sports_positions", END)

    return builder.compile()
