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
import re
from datetime import date, timedelta, datetime, timezone
from typing import Any

from langgraph.graph import StateGraph, END
from loguru import logger

from polybot.api.espn import ESPNClient, Game
from polybot.api.gamma import GammaClient
from polybot.api.odds import OddsClient, SPORT_KEYS
from polybot.config import settings
from polybot.models import Market, MarketCategory, TradeStatus
from polybot.scanner.sports_state import MatchedPair, SportsScanState
from polybot.strategies.exit import compute_exit_signals
from polybot.strategies.sports import evaluate_sports_markets, MatchedGame

# Sports leagues to scan on the global Gamma API
_SPORTS_KEYWORDS = ["NBA", "NFL", "MLB", "NHL", "FIFA", "UFC",
                    "Premier League", "Champions League", "MLS", "WNBA"]

# Min match score to consider a global ↔ US pair as the same game.
# 0.50 with token-overlap scoring; revisit after seeing real sample text.
# (Was 0.60 with a hard team-token gate — gate caused 0 matches due to
#  text-format differences between Gamma questions and US slugs/titles.)
_MIN_MATCH_SCORE = 0.50

# ESPN game status → MatchedGame status string
_ESPN_STATUS: dict[str, str] = {
    "scheduled":   "status_scheduled",
    "in_progress": "status_in_progress",
    "final":       "status_final",
}

# Words to strip when extracting team-name tokens from full-text questions
_TEAM_STOPWORDS = {
    "will", "the", "beat", "win", "vs", "against", "fc", "cf", "sc",
    "at", "in", "on", "a", "an", "be", "by", "or", "of",
}

# ── Abbreviation tables ────────────────────────────────────────────────────────
# US slugs use 2-3 letter team codes (e.g. "aec-nba-sa-chi-2025-11-10").
# These map each code to the team's distinctive keywords for overlap scoring.
# Separate dicts per sport to avoid cross-sport collisions (e.g. hou=Rockets/Astros).

_NBA_ABBREVS: dict[str, frozenset[str]] = {
    "atl": frozenset({"hawks", "atlanta"}),
    "bos": frozenset({"celtics", "boston"}),
    "bkn": frozenset({"nets", "brooklyn"}),
    "cha": frozenset({"hornets", "charlotte"}),
    "chi": frozenset({"bulls", "chicago"}),
    "cle": frozenset({"cavaliers", "cleveland"}),
    "dal": frozenset({"mavericks", "dallas"}),
    "den": frozenset({"nuggets", "denver"}),
    "det": frozenset({"pistons", "detroit"}),
    "gs":  frozenset({"warriors", "golden", "state"}),
    "gsw": frozenset({"warriors", "golden", "state"}),
    "hou": frozenset({"rockets", "houston"}),
    "ind": frozenset({"pacers", "indiana"}),
    "lac": frozenset({"clippers", "angeles"}),
    "lal": frozenset({"lakers", "angeles"}),
    "mem": frozenset({"grizzlies", "memphis"}),
    "mia": frozenset({"heat", "miami"}),
    "mil": frozenset({"bucks", "milwaukee"}),
    "min": frozenset({"timberwolves", "minnesota"}),
    "no":  frozenset({"pelicans", "orleans"}),
    "nop": frozenset({"pelicans", "orleans"}),
    "ny":  frozenset({"knicks", "york"}),
    "nyk": frozenset({"knicks", "york"}),
    "okc": frozenset({"thunder", "oklahoma"}),
    "orl": frozenset({"magic", "orlando"}),
    "phi": frozenset({"sixers", "philadelphia"}),
    "phx": frozenset({"suns", "phoenix"}),
    "pho": frozenset({"suns", "phoenix"}),
    "por": frozenset({"blazers", "portland", "trail"}),
    "sac": frozenset({"kings", "sacramento"}),
    "sa":  frozenset({"spurs", "antonio"}),
    "sas": frozenset({"spurs", "antonio"}),
    "tor": frozenset({"raptors", "toronto"}),
    "uta": frozenset({"jazz", "utah"}),
    "was": frozenset({"wizards", "washington"}),
}

_NFL_ABBREVS: dict[str, frozenset[str]] = {
    "ari": frozenset({"cardinals", "arizona"}),
    "atl": frozenset({"falcons", "atlanta"}),
    "bal": frozenset({"ravens", "baltimore"}),
    "buf": frozenset({"bills", "buffalo"}),
    "car": frozenset({"panthers", "carolina"}),
    "chi": frozenset({"bears", "chicago"}),
    "cin": frozenset({"bengals", "cincinnati"}),
    "cle": frozenset({"browns", "cleveland"}),
    "dal": frozenset({"cowboys", "dallas"}),
    "den": frozenset({"broncos", "denver"}),
    "det": frozenset({"lions", "detroit"}),
    "gb":  frozenset({"packers", "green", "bay"}),
    "hou": frozenset({"texans", "houston"}),
    "ind": frozenset({"colts", "indianapolis"}),
    "jax": frozenset({"jaguars", "jacksonville"}),
    "kc":  frozenset({"chiefs", "kansas", "city"}),
    "lac": frozenset({"chargers"}),
    "lar": frozenset({"rams", "angeles"}),
    "lv":  frozenset({"raiders", "vegas", "las"}),
    "mia": frozenset({"dolphins", "miami"}),
    "min": frozenset({"vikings", "minnesota"}),
    "ne":  frozenset({"patriots", "england"}),
    "no":  frozenset({"saints", "orleans"}),
    "nyg": frozenset({"giants", "york"}),
    "nyj": frozenset({"jets", "york"}),
    "phi": frozenset({"eagles", "philadelphia"}),
    "pit": frozenset({"steelers", "pittsburgh"}),
    "sea": frozenset({"seahawks", "seattle"}),
    "sf":  frozenset({"niners", "francisco"}),
    "tb":  frozenset({"buccaneers", "tampa"}),
    "ten": frozenset({"titans", "tennessee"}),
    "was": frozenset({"commanders", "washington"}),
}

_MLB_ABBREVS: dict[str, frozenset[str]] = {
    "ari": frozenset({"diamondbacks", "arizona"}),
    "atl": frozenset({"braves", "atlanta"}),
    "bal": frozenset({"orioles", "baltimore"}),
    "bos": frozenset({"red", "sox", "boston"}),
    "chc": frozenset({"cubs", "chicago"}),
    "chw": frozenset({"sox", "chicago", "white"}),
    "cin": frozenset({"reds", "cincinnati"}),
    "cle": frozenset({"guardians", "cleveland"}),
    "col": frozenset({"rockies", "colorado"}),
    "det": frozenset({"tigers", "detroit"}),
    "hou": frozenset({"astros", "houston"}),
    "kc":  frozenset({"royals", "kansas", "city"}),
    "laa": frozenset({"angels", "anaheim"}),
    "lad": frozenset({"dodgers", "angeles"}),
    "mia": frozenset({"marlins", "miami"}),
    "mil": frozenset({"brewers", "milwaukee"}),
    "min": frozenset({"twins", "minnesota"}),
    "nym": frozenset({"mets", "york"}),
    "nyy": frozenset({"yankees", "york"}),
    "oak": frozenset({"athletics", "oakland"}),
    "phi": frozenset({"phillies", "philadelphia"}),
    "pit": frozenset({"pirates", "pittsburgh"}),
    "sd":  frozenset({"padres", "diego"}),
    "sea": frozenset({"mariners", "seattle"}),
    "sf":  frozenset({"giants", "francisco"}),
    "stl": frozenset({"cardinals", "louis"}),
    "tb":  frozenset({"rays", "tampa"}),
    "tex": frozenset({"rangers", "texas"}),
    "tor": frozenset({"blue", "jays", "toronto"}),
    "was": frozenset({"nationals", "washington"}),
}

_NHL_ABBREVS: dict[str, frozenset[str]] = {
    "ana": frozenset({"ducks", "anaheim"}),
    "bos": frozenset({"bruins", "boston"}),
    "buf": frozenset({"sabres", "buffalo"}),
    "car": frozenset({"hurricanes", "carolina"}),
    "cbj": frozenset({"jackets", "columbus"}),
    "cgy": frozenset({"flames", "calgary"}),
    "chi": frozenset({"blackhawks", "chicago"}),
    "col": frozenset({"avalanche", "colorado"}),
    "dal": frozenset({"stars", "dallas"}),
    "det": frozenset({"wings", "detroit"}),
    "edm": frozenset({"oilers", "edmonton"}),
    "fla": frozenset({"panthers", "florida"}),
    "lak": frozenset({"kings", "angeles"}),
    "min": frozenset({"wild", "minnesota"}),
    "mtl": frozenset({"canadiens", "montreal"}),
    "njd": frozenset({"devils", "jersey"}),
    "nsh": frozenset({"predators", "nashville"}),
    "nyi": frozenset({"islanders", "york"}),
    "nyr": frozenset({"rangers", "york"}),
    "ott": frozenset({"senators", "ottawa"}),
    "phi": frozenset({"flyers", "philadelphia"}),
    "pit": frozenset({"penguins", "pittsburgh"}),
    "sea": frozenset({"kraken", "seattle"}),
    "sjs": frozenset({"sharks", "jose"}),
    "stl": frozenset({"blues", "louis"}),
    "tb":  frozenset({"lightning", "tampa"}),
    "tor": frozenset({"maple", "leafs", "toronto"}),
    "van": frozenset({"canucks", "vancouver"}),
    "vgk": frozenset({"knights", "vegas", "golden"}),
    "wsh": frozenset({"capitals", "washington"}),
    "wpg": frozenset({"jets", "winnipeg"}),
}

_SPORT_ABBREVS: dict[str, dict[str, frozenset[str]]] = {
    "nba": _NBA_ABBREVS,
    "nfl": _NFL_ABBREVS,
    "mlb": _MLB_ABBREVS,
    "nhl": _NHL_ABBREVS,
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fuzzy_score(a: str, b: str) -> float:
    """SequenceMatcher ratio between two lowercased strings."""
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _team_tokens(text: str) -> set[str]:
    """
    Extract meaningful tokens from a full-text question or team name.
    Drops stopwords and short tokens.
    """
    return {
        t.strip("?.,'-:").lower()
        for t in text.split()
        if len(t.strip("?.,'-:")) >= 3
        and t.strip("?.,'-:").lower() not in _TEAM_STOPWORDS
    }


def _slug_tokens(slug: str) -> set[str]:
    """
    Expand a US market slug into team-name keywords using sport-specific
    abbreviation tables.

    'aec-nba-sa-chi-2025-11-10' → {'spurs', 'antonio', 'bulls', 'chicago', 'nba'}

    Uses the league segment (nba/nfl/mlb/nhl) to select the right table,
    avoiding cross-sport collisions (e.g. 'hou' = Rockets in NBA, Astros in MLB).
    """
    parts = slug.lower().split("-")
    tokens: set[str] = set()

    # Identify the league segment to pick the right abbreviation table
    abbrevs: dict[str, frozenset[str]] = {}
    for part in parts:
        if part in _SPORT_ABBREVS:
            tokens.add(part)          # keep 'nba' / 'mlb' etc. as a token
            abbrevs = _SPORT_ABBREVS[part]
            break

    for part in parts:
        if part.isdigit() or part in _SPORT_ABBREVS:
            continue
        expanded = abbrevs.get(part)
        if expanded:
            tokens |= expanded
        elif len(part) >= 3:
            tokens.add(part)

    return tokens


_SLUG_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})$")


def _slug_date(slug: str) -> date | None:
    """Extract YYYY-MM-DD from the tail of a US slug, or None."""
    m = _SLUG_DATE_RE.search(slug)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _find_espn_game(question: str, games: list[Game]) -> Game | None:
    """Find the ESPN Game whose teams overlap with a market question."""
    q_tokens = _team_tokens(question)
    for game in games:
        game_tokens = _team_tokens(game.home_team) | _team_tokens(game.away_team)
        if q_tokens & game_tokens:
            return game
    return None


def _best_match(
    global_question: str,
    us_markets: list[dict],
) -> tuple[dict | None, float]:
    """
    Find the best matching US market for a global Gamma market question.

    Requires at least one shared team-name token (guards against sport-level
    false positives where title similarity is high but teams differ).
    Returns (best_market_dict, score).
    """
    if not us_markets:
        return None, 0.0

    best: dict | None = None
    best_score = 0.0
    q_tokens = _team_tokens(global_question)

    for us_mkt in us_markets:
        title = us_mkt.get("title", "") or us_mkt.get("name", "") or ""
        slug  = us_mkt.get("slug", "")

        t_tokens = _team_tokens(title) | _slug_tokens(slug)

        # Token overlap score (0 when no shared team tokens)
        overlap = len(q_tokens & t_tokens)
        token_score = overlap / max(len(q_tokens), 1)

        # String similarity score
        sim_title = _fuzzy_score(global_question, title)
        sim_slug  = _fuzzy_score(global_question, slug.replace("-", " "))

        # Token overlap acts as a tiebreaker/boost, not a hard gate.
        # Hard gate caused 0 matches when text formats differ (e.g. full name vs
        # abbreviation). Rely on _MIN_MATCH_SCORE to filter weak candidates.
        score = max(token_score, sim_title, sim_slug)

        if score > best_score:
            best_score = score
            best = us_mkt

    return best, best_score


def _extract_us_yes_price(us_market: dict) -> float | None:
    """
    Extract the YES price from a US market dict.

    Returns None (not 0.5) when the price cannot be found — callers must skip
    the pair rather than trading on a spurious 0.5 default.
    """
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
            price = outcome.get("price")
            if price is not None:
                try:
                    return float(price)
                except (TypeError, ValueError):
                    pass

    return None  # price unavailable — caller must skip


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
    try:
        async with GammaClient() as gamma:
            markets = await gamma.fetch_markets(
                limit=500,
                min_liquidity=200.0,
            )
    except Exception as exc:
        logger.warning("SPORTS Layer 1: Gamma API unreachable — skipping scan: {}", exc)
        return {"global_sports": []}

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
        # ended=False filters settled games; startDateMin drops events older than
        # yesterday so we never see November 2025 ghost markets again.
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        result = await client.list_events(
            limit=200,
            active=True,
            ended=False,
            start_date_min=yesterday,
        )
        events = result.get("events", []) if isinstance(result, dict) else result
        logger.info("SPORTS Layer 3: {} active US events", len(events))

        # Flatten events → market dicts.
        # Titles and startTime live on the event, not the market — propagate them
        # down so _best_match and _extract_us_yes_price have full context.
        markets: list[dict] = []
        for event in events:
            event_title    = event.get("title", "")
            event_slug     = event.get("slug", "")
            event_start    = event.get("startTime", "")
            event_status   = "status_scheduled" if not event.get("closed") else "status_final"
            for mkt in event.get("markets", [event]):
                enriched = dict(mkt)
                if not enriched.get("title"):
                    enriched["title"] = event_title
                if not enriched.get("slug"):
                    enriched["slug"] = event_slug
                if "startTime" not in enriched:
                    enriched["startTime"] = event_start
                if "status" not in enriched:
                    enriched["status"] = event_status
                markets.append(enriched)

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
        if us_yes_price is None:
            logger.debug("SPORTS: skipping US market with no extractable YES price: {}", us_mkt.get("slug", "?"))
            continue

        us_book_depth = _estimate_book_depth(us_mkt)
        us_slug   = us_mkt.get("slug", us_mkt.get("id", ""))
        us_title  = us_mkt.get("title", us_mkt.get("name", ""))
        us_status = us_mkt.get("status", "status_scheduled")

        # Capture ESPN game ID now so live nodes can use it as a join key
        # without re-doing team-name matching on every scan.
        # today_games not yet available at match_markets time — ID is populated
        # in run_sports_strategy and live_sports_graph after ESPN fetch.
        pairs.append(MatchedPair(
            global_market=global_mkt,
            us_slug=us_slug,
            us_title=us_title,
            us_yes_price=us_yes_price,
            us_book_depth=us_book_depth,
            us_status=us_status,
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

    # Build odds lookup keyed by us_slug using token-based team matching
    # (substring matching fails on "Memphis Grizzlies" vs question "Will Grizzlies…")
    odds_by_game: dict = {}
    for odds in state.odds_data:
        odds_tokens = _team_tokens(odds.home_team) | _team_tokens(odds.away_team)
        for pair in state.matched_pairs:
            if _team_tokens(pair.global_market.question) & odds_tokens:
                odds_by_game[pair.us_slug] = odds
                break

    # Convert MatchedPair → MatchedGame, enriching with ESPN status/teams.
    # Also backfill espn_game_id on the pair so live nodes have the join key.
    matched_games: list[MatchedGame] = []
    for pair in state.matched_pairs:
        espn_game = _find_espn_game(pair.global_market.question, state.today_games)
        if espn_game and not pair.espn_game_id:
            pair.espn_game_id = espn_game.game_id
        matched_games.append(MatchedGame(
            global_market=pair.global_market,
            us_slug=pair.us_slug,
            us_yes_price=pair.us_yes_price,
            us_book_depth=pair.us_book_depth,
            game_start=(
                espn_game.commence_time
                if espn_game
                else pair.global_market.end_date
            ),
            status=(
                _ESPN_STATUS.get(espn_game.status, pair.us_status)
                if espn_game
                else pair.us_status   # from US event dict; never blindly default
            ),
            home_team=espn_game.home_team if espn_game else "",
            away_team=espn_game.away_team if espn_game else "",
        ))

    # Kelly sizing inputs
    open_exposure = sum(
        t.size_usd for t in state.open_positions
        if t.status == TradeStatus.OPEN and t.live_platform == "polymarket_us"
    )

    opportunities = evaluate_sports_markets(
        matched_pairs=matched_games,
        odds_by_game=odds_by_game,
        injuries=state.injuries,
        today_games=state.today_games,
        yesterday_games=state.yesterday_games,
        bankroll=settings.paper_starting_balance,
        open_exposure=open_exposure,
        min_edge=settings.sports_min_edge,
    )

    logger.info(
        "SPORTS strategy: {} pairs → {} opportunities",
        len(state.matched_pairs), len(opportunities),
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

    # Key alignment: PaperTrade.market_id == Opportunity.market.id == global_market.id
    # Sports positions use US prices for current value, but are identified by the
    # global Gamma market ID (which is what the paper trader stores in market_id).
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
        try:
            async with GammaClient() as gamma:
                for mid in missing_ids:
                    try:
                        m = await gamma.fetch_market_by_id(mid)
                        if m:
                            current_prices[mid] = m.yes_price
                            hours_to_close[mid] = m.hours_until_close
                    except Exception as e:
                        logger.warning("SPORTS: could not fetch market {}: {}", mid[:8], e)
        except Exception as exc:
            logger.warning("SPORTS: Gamma unreachable for stale price refresh: {}", exc)

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
