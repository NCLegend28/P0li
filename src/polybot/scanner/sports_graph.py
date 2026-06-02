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
  Order execution happens in trading/engine.py via PolymarketUSClient (sync).
"""

from __future__ import annotations

import asyncio
import difflib
import re
from datetime import date, timedelta, datetime, timezone
from typing import Any

from langgraph.graph import StateGraph, END
from loguru import logger

# Tracks the last time The Odds API was called; None = never.
# Enforces settings.odds_poll_interval_seconds between calls.
_last_odds_fetch: datetime | None = None

from polybot.api.espn import ESPNClient, Game
from polybot.api.gamma import GammaClient
from polybot.api.odds import OddsClient, SPORT_KEYS
from polybot.config import settings
from polybot.models import LiveGameContext, Market, MarketCategory, TradeStatus
from polybot.scanner.sports_state import MatchedPair, SportsScanState
from polybot.strategies.exit import compute_exit_signals
from polybot.strategies.llm_picker import pick_opportunities
from polybot.strategies.sports import evaluate_sports_markets, MatchedGame
from polybot.strategies.us_direct import USDirectStrategy, USEvent
from polybot.strategies.delay_arb import DelayArbitrageStrategy

# Sports leagues to scan on the global Gamma API
_SPORTS_KEYWORDS = ["NBA", "NFL", "MLB", "NHL", "FIFA", "UFC",
                    "Premier League", "Champions League", "MLS", "WNBA",
                    "EPL", "UCL"]

# Per-league text markers — case-insensitive substrings that identify a league
# from a market question, US event title, or slug. Used by the v1 scope filter
# (`settings.sports_league_set`) to drop markets outside the enabled leagues.
#
# Ambiguous mascots (Rangers, Giants, Cardinals — shared between leagues) are
# deliberately omitted; the league acronym in question text is a reliable
# fallback for those games.
_LEAGUE_MARKERS: dict[str, tuple[str, ...]] = {
    "NBA":  ("nba", "lakers", "celtics", "warriors", "nuggets", "bucks",
             "76ers", "sixers", "knicks", "thunder", "timberwolves",
             "mavericks", "suns", "clippers", "pacers", "magic", "bulls",
             "raptors", "hawks", "hornets", "cavaliers", "cavs", "pistons",
             "rockets", "grizzlies", "pelicans", "spurs", "trail blazers",
             "jazz", "wizards", "heat", "nets"),
    "MLB":  ("mlb", "yankees", "red sox", "dodgers", "mets",
             "cubs", "braves", "astros", "phillies", "padres", "mariners",
             "blue jays", "orioles", "guardians", "tigers",
             "white sox", "royals", "twins", "athletics", "angels",
             "rays", "marlins", "nationals", "pirates", "reds", "brewers",
             "rockies", "diamondbacks"),
    "EPL":  ("epl", "premier league", "liverpool", "arsenal", "chelsea",
             "tottenham", "newcastle", "manchester city",
             "manchester united", "everton", "aston villa",
             "west ham", "brighton"),
    "UCL":  ("ucl", "champions league", "real madrid", "barcelona",
             "bayern munich", "psg", "paris saint-germain",
             "juventus", "inter milan", "ac milan", "atletico madrid"),
    "MLS":  ("mls", "lafc", "inter miami", "la galaxy",
             "seattle sounders", "atlanta united", "nycfc"),
    "FIFA": ("fifa", "world cup", "copa america", "uefa euro"),
    # Out-of-scope-by-default leagues are still recognised so they can be
    # opted back in via SPORTS_LEAGUES.
    "NHL":  ("nhl", "maple leafs", "canadiens", "bruins", "blackhawks",
             "penguins", "flyers", "capitals", "avalanche", "oilers",
             "canucks", "panthers", "lightning"),
    "NFL":  ("nfl", "super bowl", "patriots", "chiefs", "cowboys",
             "packers", "eagles", "steelers", "ravens", "49ers",
             "broncos", "seahawks", "rams"),
    "WNBA": ("wnba", "liberty", "aces", "sky", "fever", "storm"),
    "UFC":  ("ufc", "mma"),
}

# Compile each league's markers as a single word-bounded, case-insensitive
# regex so short acronyms ("nba") don't accidentally match inside longer words
# ("wnba"), and team-name phrases ("premier league") still match correctly.
_LEAGUE_PATTERNS: dict[str, re.Pattern[str]] = {
    code: re.compile(
        r"\b(?:" + "|".join(re.escape(m) for m in markers) + r")\b",
        re.IGNORECASE,
    )
    for code, markers in _LEAGUE_MARKERS.items()
}


def _matched_leagues(text: str) -> frozenset[str]:
    """Return league codes whose markers appear (case-insensitive, word-bounded) in `text`."""
    if not text:
        return frozenset()
    return frozenset(
        code for code, pat in _LEAGUE_PATTERNS.items()
        if pat.search(text)
    )


def _in_enabled_leagues(text: str, enabled: frozenset[str]) -> bool:
    """True if any marker for an enabled league appears in `text`."""
    if not enabled:
        return False
    return bool(_matched_leagues(text) & enabled)

# Min match score to consider a global ↔ US pair as the same game.
# 0.50 with token-overlap scoring; revisit after seeing real sample text.
# (Was 0.60 with a hard team-token gate — gate caused 0 matches due to
#  text-format differences between Gamma questions and US slugs/titles.)
_MIN_MATCH_SCORE = 0.30

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

    enabled = settings.sports_league_set
    sports_markets = [
        m for m in markets
        if m.category == MarketCategory.SPORTS
        and m.hours_until_close >= 1.0
        and 0.05 <= m.yes_price <= 0.95
        and _in_enabled_leagues(m.question, enabled)
    ]

    logger.info(
        "SPORTS Layer 1: {}/{} global markets qualify "
        "(sports, active, priced, leagues={})",
        len(sports_markets), len(markets), ",".join(sorted(enabled)) or "<none>",
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
        # Skip events whose title/slug don't match an enabled league (v1 scope).
        enabled = settings.sports_league_set
        markets: list[dict] = []
        kept_events = 0
        for event in events:
            event_title    = event.get("title", "")
            event_slug     = event.get("slug", "")
            if not _in_enabled_leagues(f"{event_title} {event_slug}", enabled):
                continue
            kept_events += 1
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

        logger.info(
            "SPORTS Layer 3: {}/{} events kept after league filter ({} markets)",
            kept_events, len(events), len(markets),
        )
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

    enabled = settings.sports_league_set

    # ── The Odds API (Layer 2) ────────────────────────────────────────────────
    global _last_odds_fetch
    _odds_due = (
        _last_odds_fetch is None
        or (datetime.now(timezone.utc) - _last_odds_fetch).total_seconds()
        >= settings.odds_poll_interval_seconds
    )

    if settings.odds_api_key and _odds_due:
        odds_client = OddsClient(api_key=settings.odds_api_key)
        active_sports = {
            keyword
            for pair in state.matched_pairs
            for keyword in _SPORTS_KEYWORDS
            if keyword.upper() in pair.global_market.question.upper()
            and keyword.upper() in SPORT_KEYS
            and keyword.upper() in enabled
        }

        for sport in list(active_sports)[:3]:   # cap at 3 sports to preserve quota
            try:
                game_odds = await odds_client.fetch_odds(sport)
                odds_list.extend(game_odds)
                logger.info("SPORTS Layer 2: {} {} games from Odds API", len(game_odds), sport)
            except Exception as e:
                logger.warning("SPORTS Layer 2: Odds API failed for {}: {}", sport, e)
        _last_odds_fetch = datetime.now(timezone.utc)
    elif settings.odds_api_key:
        secs_left = int(
            settings.odds_poll_interval_seconds
            - (datetime.now(timezone.utc) - _last_odds_fetch).total_seconds()
        )
        logger.debug("SPORTS Layer 2: Odds API throttled — next poll in {}s", secs_left)
    else:
        logger.debug("SPORTS Layer 2: ODDS_API_KEY not set — using Layer 1 alone (conf=0.7)")

    # ── ESPN schedule + injuries ──────────────────────────────────────────────
    # NBA/MLB/NHL/NFL have ESPN schedule + injury feeds. Soccer leagues
    # (EPL/UCL/MLS) have schedules via espn_live but no injury feed parser yet,
    # so they're omitted here — soccer opportunities still surface, just
    # without B2B/injury adjustments.
    espn = ESPNClient()
    _ESPN_LEAGUES = ("NBA", "NFL", "MLB", "NHL")
    active_leagues = {
        keyword
        for pair in state.matched_pairs
        for keyword in _ESPN_LEAGUES
        if keyword in pair.global_market.question.upper()
        and keyword in enabled
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


# ─── Node: enrich_with_vault ─────────────────────────────────────────────────
# Module-level singleton — vault lookups are filesystem reads with internal
# caching; one client across scans is fine and avoids re-stat'ing every loop.
_VAULT_CLIENT: Any = None


def _get_vault_client():
    global _VAULT_CLIENT
    if _VAULT_CLIENT is None:
        from polybot.api.vault import VaultClient
        _VAULT_CLIENT = VaultClient(
            vault_root        = settings.vault_path or None,
            cache_ttl_seconds = settings.vault_cache_seconds,
        )
    return _VAULT_CLIENT


def _pair_sport(pair: MatchedPair) -> str | None:
    """
    Identify the sport code for a matched pair by checking which enabled league
    markers appear in its text. Returns one of NBA / MLB / EPL / UCL / MLS /
    FIFA / etc., or None when no marker hits.
    """
    enabled = settings.sports_league_set
    text = f"{pair.global_market.question} {pair.us_title}"
    leagues = _matched_leagues(text) & enabled
    if not leagues:
        return None
    # Stable ordering so the same pair always resolves to the same sport.
    return sorted(leagues)[0]


async def enrich_with_vault(state: SportsScanState) -> dict[str, Any]:
    """
    Attach VaultContext to each MatchedPair for both teams when a page exists.

    Team identity is resolved by checking the matched ESPN game (when found)
    and falling back to parsing the question for known team mascots. Missing
    vault pages are fine — vault context is purely additive.
    """
    if not state.matched_pairs:
        return {}

    vault = _get_vault_client()
    if not vault.is_enabled():
        return {}

    hits = 0
    for pair in state.matched_pairs:
        sport = _pair_sport(pair)
        if sport is None:
            continue

        # Prefer ESPN-derived team names (canonical form: "Los Angeles Lakers")
        espn_game = _find_espn_game(pair.global_market.question, state.today_games)
        if espn_game is not None:
            home_name = espn_game.home_team
            away_name = espn_game.away_team
        else:
            # Fall back: extract two mascot candidates from the question text
            home_name, away_name = _extract_team_mascots_from_question(
                pair.global_market.question, sport
            )

        if home_name:
            ctx_home = vault.get_team_context(sport, home_name)
            if ctx_home is not None:
                pair.vault_home = ctx_home
                hits += 1
        if away_name:
            ctx_away = vault.get_team_context(sport, away_name)
            if ctx_away is not None:
                pair.vault_away = ctx_away
                hits += 1

    logger.info(
        "SPORTS vault: {} team-page hits across {} pairs",
        hits, len(state.matched_pairs),
    )
    return {}


def _extract_team_mascots_from_question(question: str, sport: str) -> tuple[str, str]:
    """
    Pull (home_mascot, away_mascot) from a question text using the league's
    marker list. Returns the first two distinct, multi-character markers
    found in order. The "first mascot" heuristic from live_sports.py treats
    the first-mentioned team as the subject (YES side / typically home).

    Falls back to ("", "") when fewer than two team markers are found.
    """
    markers = _LEAGUE_MARKERS.get(sport, ())
    q = (question or "").lower()
    hits: list[tuple[int, str]] = []
    seen: set[str] = set()
    for m in markers:
        if len(m) < 4:   # skip league acronyms like "nba", "mlb"
            continue
        idx = q.find(m)
        if idx >= 0 and m not in seen:
            hits.append((idx, m))
            seen.add(m)
    hits.sort()
    if len(hits) >= 2:
        return hits[0][1], hits[1][1]
    if len(hits) == 1:
        return hits[0][1], ""
    return "", ""


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
            vault_home=pair.vault_home,
            vault_away=pair.vault_away,
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
        bankroll=settings.simulated_starting_balance,
        open_exposure=open_exposure,
        min_edge=settings.sports_min_edge,
    )

    logger.info(
        "SPORTS strategy: {} pairs → {} opportunities",
        len(state.matched_pairs), len(opportunities),
    )

    return {"opportunities": opportunities}


# ─── Node: monitor_sports_positions ──────────────────────────────────────────

async def fetch_live_states(state: SportsScanState) -> dict[str, Any]:
    """
    For matched pairs that back an open position, fetch live ESPN game state
    and attach a LiveGameContext to each pair. Powers the in-game exit logic
    (BLOWOUT_STOP, SCORE_REVERSAL, GAME_ENDED) in monitor_sports_positions.

    Skipped entirely when there are no open US sports positions — live fetches
    are wasted bandwidth if there's nothing to monitor.
    """
    open_sports_ids = {
        t.market_id for t in state.open_positions
        if t.live_platform == "polymarket_us"
    }
    if not open_sports_ids:
        return {"live_game_states": []}

    target_pairs = [
        p for p in state.matched_pairs
        if p.global_market.id in open_sports_ids
    ]
    if not target_pairs:
        return {"live_game_states": []}

    # ESPN live scoreboard supports these leagues; FIFA tournaments aren't on
    # the scoreboard endpoint (use individual competition paths instead — out
    # of scope for v1). Soccer leagues that ARE supported still work.
    _ESPN_LIVE_LEAGUES = {"NBA", "MLB", "EPL", "UCL", "MLS", "NHL", "NFL", "WNBA"}
    enabled = settings.sports_league_set

    leagues_to_fetch: set[str] = set()
    for p in target_pairs:
        text = f"{p.global_market.question} {p.us_title}"
        leagues_to_fetch.update(_matched_leagues(text) & enabled & _ESPN_LIVE_LEAGUES)

    if not leagues_to_fetch:
        return {"live_game_states": []}

    from polybot.api.espn_live import ESPNLiveClient

    espn_live = ESPNLiveClient(poll_interval=settings.espn_live_poll_interval)
    all_live: list[LiveGameContext] = []
    for lg in sorted(leagues_to_fetch):
        try:
            games = await espn_live.fetch_all_live(lg)
            all_live.extend(games)
        except Exception as e:
            logger.warning("SPORTS live: ESPN fetch failed for {}: {}", lg, e)

    # Attach a LiveGameContext to each target pair by team-token overlap.
    attached = 0
    for pair in target_pairs:
        q_tokens = _team_tokens(pair.global_market.question) | _team_tokens(pair.us_title)
        best, best_score = None, 0
        for ctx in all_live:
            ctx_tokens = _team_tokens(ctx.home_team) | _team_tokens(ctx.away_team)
            score = len(q_tokens & ctx_tokens)
            if score > best_score:
                best, best_score = ctx, score
        if best is not None and best_score >= 1:
            pair.live_context = best
            attached += 1

    logger.info(
        "SPORTS live: {} live games across {} leagues, attached to {}/{} target pairs",
        len(all_live), len(leagues_to_fetch), attached, len(target_pairs),
    )
    return {"live_game_states": all_live}


async def monitor_sports_positions(state: SportsScanState) -> dict[str, Any]:
    """
    Check open sports positions for exit conditions.

    Runs two exit engines:
      1. Standard (exit.py) — profit target, edge collapse, time stop,
         pregame lock. Driven by current US price + hours remaining.
      2. Live in-game (live_sports.py + exit.py) — GAME_ENDED, BLOWOUT_STOP,
         SCORE_REVERSAL. Driven by ESPN live state attached in
         fetch_live_states.

    Live signals win when both engines fire on the same trade — they're more
    specific and reference actual game state instead of price heuristics.
    """
    if not state.open_positions:
        return {"exit_signals": [], "live_exit_signals": []}

    # Key alignment: TradeRecord.market_id == Opportunity.market.id == global_market.id
    # Sports positions use US prices for current value, but are identified by the
    # global Gamma market ID (which is what the trading engine stores in market_id).
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

    # ── Standard exit signals (price- and time-based) ─────────────────────────
    signals = compute_exit_signals(
        open_trades=sports_positions,
        current_prices=current_prices,
        hours_to_close=hours_to_close,
    )

    # ── Live in-game exit signals ─────────────────────────────────────────────
    from polybot.strategies.exit import compute_live_exit_signals
    from polybot.strategies.live_sports import compute_live_model_probs

    model_probs, live_contexts = compute_live_model_probs(
        open_trades=sports_positions,
        matched_pairs=state.matched_pairs,
    )
    live_signals = compute_live_exit_signals(
        open_trades=sports_positions,
        current_prices=current_prices,
        live_contexts=live_contexts,
        model_probs=model_probs,
    )

    # Dedup: when a live signal fires on the same trade as a standard signal,
    # the live one wins (it carries the game-state context the user wants).
    live_trade_ids = {s.trade_id for s in live_signals}
    standard_filtered = [s for s in signals if s.trade_id not in live_trade_ids]
    merged = live_signals + standard_filtered

    if merged:
        logger.info(
            "SPORTS: {} exit signals ({} live + {} standard) generated",
            len(merged), len(live_signals), len(standard_filtered),
        )

    return {"exit_signals": merged, "live_exit_signals": live_signals}

# ─── Node: run_us_direct_strategy ───────────────────────────────────────────

async def run_us_direct_strategy(state: SportsScanState) -> dict[str, Any]:
    """
    Evaluate US-only markets for direct trading opportunities.
    
    This runs in parallel with cross-platform arbitrage and trades
    purely on US Polymarket vs sportsbook odds (no global CLOB needed).
    """
    if not state.us_events:
        logger.debug("US direct: no US events available")
        return {"us_opportunities": [], "delay_opportunities": []}
    
    # Convert US events to USEvent objects
    us_events: list[USEvent] = []
    for evt in state.us_events:
        us_events.append(USEvent(
            slug=evt.get("slug", ""),
            title=evt.get("title", ""),
            yes_price=evt.get("yes_price", 0.5),
            no_price=evt.get("no_price", 0.5),
            volume=evt.get("volume", 0),
            game_start=evt.get("game_start", datetime.now(timezone.utc)),
            sport=evt.get("sport", ""),
            home_team=evt.get("home_team", ""),
            away_team=evt.get("away_team", ""),
        ))
    
    # Build odds lookup
    odds_by_game: dict = {}
    for odds in state.odds_data:
        odds_teams = set(
            (odds.home_team or "").lower().split() + 
            (odds.away_team or "").lower().split()
        )
        for evt in us_events:
            evt_teams = set(
                (evt.home_team or "").lower().split() + 
                (evt.away_team or "").lower().split()
            )
            if odds_teams & evt_teams:
                odds_by_game[evt.slug] = odds
                break
    
    # Calculate open exposure
    open_exposure = sum(
        t.size_usd for t in state.open_positions
        if t.status == TradeStatus.OPEN and t.live_platform == "polymarket_us"
    )
    
    # Run US direct strategy
    us_strategy = USDirectStrategy(min_edge=settings.sports_min_edge)
    us_opportunities = us_strategy.evaluate_batch(
        us_events=us_events,
        odds_by_game=odds_by_game,
        bankroll=settings.simulated_starting_balance,
        open_exposure=open_exposure,
    )
    
    # Run delay arbitrage strategy (independent, filters overlaps)
    delay_strategy = DelayArbitrageStrategy(
        min_edge=settings.sports_min_edge * 0.8,  # Slightly lower threshold
        min_movement=0.03,
        cooldown_minutes=30.0,
    )
    existing_opp_ids = [o.id for o in us_opportunities]
    delay_opportunities = delay_strategy.evaluate_batch(
        us_events=us_events,
        odds_by_game=odds_by_game,
        existing_opportunities=existing_opp_ids,
        bankroll=settings.simulated_starting_balance,
        open_exposure=open_exposure,
    )
    
    return {"us_opportunities": us_opportunities, "delay_opportunities": delay_opportunities}


# ─── Node: llm_pick_sports ────────────────────────────────────────────────────

async def llm_pick_sports(state: SportsScanState) -> dict[str, Any]:
    """
    LLM intuition filter over all three sports opportunity streams.

    Runs a single LLM pass per stream so the model sees the full batch in
    context. Disabled by default (settings.llm_picker_enabled). When
    disabled or on error, returns the input streams unchanged.
    """
    out: dict[str, Any] = {}
    if state.opportunities:
        out["opportunities"] = await pick_opportunities(
            state.opportunities, scan_number=state.scan_number,
        )
    if state.us_opportunities:
        out["us_opportunities"] = await pick_opportunities(
            state.us_opportunities, scan_number=state.scan_number,
        )
    if state.delay_opportunities:
        out["delay_opportunities"] = await pick_opportunities(
            state.delay_opportunities, scan_number=state.scan_number,
        )
    return out


def build_sports_scanner_graph() -> Any:
    """
    Assemble the sports scanner LangGraph pipeline.

    Two parallel paths after fetching data:
    
    Path A (Arbitrage):
      fetch_global_sports → fetch_us_events → match_markets
        → fetch_odds_and_schedule → run_sports_strategy
          → monitor_sports_positions → END
    
    Path B (US Direct):
      fetch_us_events (shared) → fetch_odds_and_schedule (shared)
        → run_us_direct_strategy → monitor_sports_positions → END
    """
    builder = StateGraph(SportsScanState)

    # Data fetching nodes
    builder.add_node("fetch_global_sports",      fetch_global_sports)
    builder.add_node("fetch_us_events",          fetch_us_events)
    builder.add_node("match_markets",            match_markets)
    builder.add_node("fetch_odds_and_schedule",  fetch_odds_and_schedule)

    # Vault enrichment (Obsidian sports knowledge layer)
    builder.add_node("enrich_with_vault",        enrich_with_vault)

    # Strategy nodes
    builder.add_node("run_sports_strategy",      run_sports_strategy)
    builder.add_node("run_us_direct_strategy",   run_us_direct_strategy)
    builder.add_node("llm_pick_sports",          llm_pick_sports)

    # Live in-game state + position monitoring
    builder.add_node("fetch_live_states",        fetch_live_states)
    builder.add_node("monitor_sports_positions", monitor_sports_positions)

    # Sequential pipeline:
    #   discovery → vault enrichment → pre-game arb → US-direct
    #   → live state → monitor (incl. live exits)
    # enrich_with_vault runs AFTER fetch_odds_and_schedule (which provides
    # today_games for team-name resolution) and BEFORE run_sports_strategy
    # (so vault flags can nudge Opportunity confidence).
    # fetch_live_states runs only when there are open US sports positions;
    # otherwise it returns empty quickly.
    builder.set_entry_point("fetch_global_sports")
    builder.add_edge("fetch_global_sports",      "fetch_us_events")
    builder.add_edge("fetch_us_events",          "match_markets")
    builder.add_edge("match_markets",            "fetch_odds_and_schedule")
    builder.add_edge("fetch_odds_and_schedule",  "enrich_with_vault")
    builder.add_edge("enrich_with_vault",        "run_sports_strategy")
    builder.add_edge("run_sports_strategy",      "run_us_direct_strategy")
    builder.add_edge("run_us_direct_strategy",   "llm_pick_sports")
    builder.add_edge("llm_pick_sports",          "fetch_live_states")
    builder.add_edge("fetch_live_states",        "monitor_sports_positions")

    builder.add_edge("monitor_sports_positions", END)

    return builder.compile()
