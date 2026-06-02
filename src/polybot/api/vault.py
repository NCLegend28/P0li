"""
Vault adapter — reads sports team pages from the user's Obsidian vault.

The vault sits outside this repo (path comes from `settings.vault_path`).
Pages live at `wiki/areas/sports/<sport>/teams/<team>.md` and follow the
schema documented at `wiki/areas/sports/_overview.md`:

    ---
    type: area
    sport: nba
    team: lakers
    flags: [key-player-out]
    form: "W W L W L"
    key_players:
      - name: LeBron James
        status: questionable
        notes: "Hamstring tightness"
    last_observed: 2026-05-15
    ---

The adapter parses that frontmatter into a `VaultContext` the scanner can
attach to a `MatchedPair`. The body of the page is excerpted for inclusion
in alerts so the user sees the narrative context alongside the numeric edge.

Design notes:
  * Fail-open. If the vault doesn't exist, isn't readable, or has no page
    for a team, return None. The bot must run identically without a vault —
    vault context only ever ADDS information.
  * Cached for 10 minutes per (sport, team) key. The vault is updated by
    hand or via ingest; the bot should not stat the filesystem every 30s.
  * No writes. Per the vault's CLAUDE.md, the user reads sources and the
    ingest workflow writes pages. The bot only reads.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from loguru import logger

try:
    import yaml
except ImportError:  # pragma: no cover — yaml comes in transitively via langchain-core
    yaml = None


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PlayerStatus:
    """One key player's current availability + a short note."""
    name:   str
    status: str = "active"   # active | questionable | doubtful | out
    notes:  str = ""


@dataclass(frozen=True)
class VaultContext:
    """Structured snapshot of what the vault knows about a team."""
    team:           str               # slug from frontmatter (e.g. "lakers")
    sport:          str               # "nba" | "mlb" | "soccer"
    path:           Path
    flags:          tuple[str, ...]   # see _overview.md for the recognised set
    form:           str               # free text — "W W L W L" or a sentence
    key_players:    tuple[PlayerStatus, ...]
    last_observed:  date | None
    body_excerpt:   str               # first ~400 chars of body for alerts
    is_stale:       bool              # True if last_observed > 14 days ago
    age_days:       int | None        # None if last_observed missing


# ─── Constants ────────────────────────────────────────────────────────────────

_STALE_AFTER_DAYS = 14
_BODY_EXCERPT_CHARS = 400
_CACHE_TTL_SECONDS  = 600

# Sport-code → vault sub-directory mapping. WNBA folds into NBA's directory
# because pages there cover both leagues; soccer collapses the various
# Polymarket sport codes onto one folder since they share clubs.
_SPORT_TO_DIR: dict[str, str] = {
    "NBA":  "nba",
    "WNBA": "nba",
    "MLB":  "mlb",
    "EPL":  "soccer",
    "UCL":  "soccer",
    "MLS":  "soccer",
    "FIFA": "soccer",
}


# ─── Client ───────────────────────────────────────────────────────────────────

class VaultClient:
    """Read-only adapter for the sports area of the Obsidian vault."""

    def __init__(self, vault_root: str | Path | None, *, cache_ttl_seconds: int = _CACHE_TTL_SECONDS):
        # None / empty → no vault configured; client returns None for all lookups.
        self.vault_root: Path | None = Path(vault_root).expanduser() if vault_root else None
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[tuple[str, str], tuple[VaultContext | None, float]] = {}

        if self.vault_root is None:
            logger.debug("VaultClient: no vault_path configured — adapter is a no-op")
        elif not self.vault_root.is_dir():
            logger.warning("VaultClient: vault_path {} not found — adapter is a no-op", self.vault_root)
            self.vault_root = None

    # ── Public API ────────────────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        return self.vault_root is not None and yaml is not None

    def get_team_context(self, sport: str, team_name: str) -> Optional[VaultContext]:
        """
        Look up a team's vault page. Returns None if no page is found or the
        vault isn't configured. Results cached for `cache_ttl_seconds`.
        """
        if not self.is_enabled() or not team_name:
            return None

        sport_dir = _SPORT_TO_DIR.get(sport.upper())
        if sport_dir is None:
            return None

        key = (sport_dir, team_name.lower())
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached and (now - cached[1]) < self._cache_ttl:
            return cached[0]

        path = self._find_team_page(sport_dir, team_name)
        ctx = self._parse_team_page(path, sport_dir) if path else None
        self._cache[key] = (ctx, now)
        return ctx

    def clear_cache(self) -> None:
        """Drop the in-memory cache. Useful for tests + after ingest runs."""
        self._cache.clear()

    # ── Page resolution ───────────────────────────────────────────────────────

    def _find_team_page(self, sport_dir: str, team_name: str) -> Path | None:
        assert self.vault_root is not None
        teams_dir = self.vault_root / "wiki" / "areas" / "sports" / sport_dir / "teams"
        if not teams_dir.is_dir():
            return None
        for cand in _team_slug_candidates(team_name):
            path = teams_dir / f"{cand}.md"
            if path.is_file():
                return path
        return None

    def _parse_team_page(self, path: Path, sport_dir: str) -> VaultContext | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("VaultClient: could not read {}: {}", path, e)
            return None

        frontmatter, body = _split_frontmatter(raw)

        flags_raw = frontmatter.get("flags") or []
        flags: tuple[str, ...] = tuple(str(f).strip() for f in flags_raw if str(f).strip())

        form = str(frontmatter.get("form") or "").strip()

        key_players_raw = frontmatter.get("key_players") or []
        key_players: list[PlayerStatus] = []
        for p in key_players_raw:
            if not isinstance(p, dict):
                continue
            key_players.append(PlayerStatus(
                name   = str(p.get("name", "")).strip(),
                status = str(p.get("status", "active")).strip().lower(),
                notes  = str(p.get("notes", "")).strip(),
            ))

        last_obs_raw = frontmatter.get("last_observed")
        last_obs: date | None = None
        if last_obs_raw:
            try:
                last_obs = date.fromisoformat(str(last_obs_raw))
            except ValueError:
                pass

        age_days: int | None = None
        is_stale = False
        if last_obs:
            age_days = (date.today() - last_obs).days
            is_stale = age_days > _STALE_AFTER_DAYS

        body_excerpt = _body_excerpt(body, max_chars=_BODY_EXCERPT_CHARS)
        team_slug = str(frontmatter.get("team") or path.stem)

        return VaultContext(
            team          = team_slug,
            sport         = sport_dir,
            path          = path,
            flags         = flags,
            form          = form,
            key_players   = tuple(key_players),
            last_observed = last_obs,
            body_excerpt  = body_excerpt,
            is_stale      = is_stale,
            age_days      = age_days,
        )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _team_slug_candidates(team_name: str) -> list[str]:
    """
    Generate candidate file slugs for a team name. Tried in order.

    Examples:
      "Los Angeles Lakers"   → ["los-angeles-lakers", "lakers", "los"]
      "Yankees"              → ["yankees"]
      "Manchester City"      → ["manchester-city", "city", "manchester"]
      "Inter Miami CF"       → ["inter-miami-cf", "cf", "inter"]   # cf collision filtered below

    Single-letter or 1-2 char tail tokens are dropped (avoids "fc"/"cf").
    """
    normalized = re.sub(r"[^a-z0-9\s-]", "", (team_name or "").lower()).strip()
    if not normalized:
        return []
    full_slug = re.sub(r"\s+", "-", normalized)
    tokens = normalized.split()

    candidates: list[str] = [full_slug]
    if len(tokens) > 1:
        last = tokens[-1]
        first = tokens[0]
        # Drop suffix tokens that are obvious club designators rather than names
        _CLUB_SUFFIXES = {"fc", "cf", "sc", "afc", "ac", "cb"}
        if len(last) >= 3 and last not in _CLUB_SUFFIXES:
            candidates.append(last)
        if len(first) >= 3 and first not in _CLUB_SUFFIXES:
            candidates.append(first)

    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _split_frontmatter(raw: str) -> tuple[dict, str]:
    """Parse leading `---`-delimited YAML frontmatter. Returns ({} , raw) when absent."""
    if not raw.startswith("---"):
        return {}, raw
    if yaml is None:
        return {}, raw
    end_match = re.search(r"\n---\s*\n", raw[3:])
    if not end_match:
        return {}, raw
    yaml_text = raw[3:3 + end_match.start()]
    body = raw[3 + end_match.end():]
    try:
        data = yaml.safe_load(yaml_text) or {}
        if not isinstance(data, dict):
            data = {}
    except yaml.YAMLError as e:
        logger.warning("VaultClient: malformed frontmatter — {}", e)
        data = {}
    return data, body


def _body_excerpt(body: str, *, max_chars: int) -> str:
    """First ~max_chars of meaningful body text, with the H1 stripped."""
    body = (body or "").strip()
    if body.startswith("#"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    body = body.strip()
    if len(body) <= max_chars:
        return body
    cut = body[:max_chars].rsplit(" ", 1)[0]
    return cut + "…"
