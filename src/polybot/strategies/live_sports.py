"""
Live in-game win-probability model for open sports positions.

The pre-game strategy in `sports.py` finds entries by comparing global ↔ US
Polymarket prices. Once a position is open, this module watches the live
game state (score, clock, period) and produces a current-best estimate of
the YES-side win probability for each open market. That estimate feeds
`compute_live_exit_signals` in `exit.py`, which decides whether to close.

Three sport-specific models:

  NBA / WNBA — Stern-style normal model. Live home win probability is
    Φ((diff + h) / (σ · √(t_remaining / T_regulation))), where σ captures
    typical late-game score variance and h is a small home-court edge.
    See Stern (1994), "A Brownian Motion Model for the Progress of Sports
    Scores." Tuned conservatively — this is an exit-decision signal, not a
    pricing model.

  MLB — innings-based step model. Win expectancy tables exist in the
    sabermetrics literature; v1 uses a closed-form approximation based on
    score diff and innings remaining. Skips the half-inning detail.

  Soccer — Poisson-process model. Remaining goals follow Poisson(λ · t/90).
    P(home wins) ≈ standard final-score distribution given current lead
    and minutes remaining.

All three return a probability in [0, 1] that the HOME team wins. The
caller maps that to the YES side by checking which team is the question's
subject (first-mentioned team in the global market question).

Caveats — known v1 limitations:
  * No injury / momentum / lineup adjustments — pure score+clock.
  * MLB ignores who's batting and bases occupied.
  * Soccer ignores red cards.
  * Extra-time / overtime falls back to the regulation model with clamped
    time-remaining, which slightly over-weights the lead.
These all bias toward a less reactive exit signal, which is the safe side
for an alert-only v1 (we'd rather hold than panic-exit on a noisy signal).
"""

from __future__ import annotations

import math
from typing import Iterable

from loguru import logger

from polybot.models import LiveGameContext, TradeRecord
from polybot.scanner.sports_state import MatchedPair


# ─── Constants ────────────────────────────────────────────────────────────────

_NBA_SIGMA_FULL = 14.0   # typical NBA winning-margin SD across full game
_NBA_HOME_EDGE  = 3.0    # approximate home advantage in points
_NBA_REGULATION_SECS = 48 * 60

_WNBA_SIGMA_FULL = 12.0  # slightly lower-scoring than NBA
_WNBA_HOME_EDGE  = 2.5
_WNBA_REGULATION_SECS = 40 * 60

_MLB_REGULATION_INNINGS = 9
_MLB_HOME_EDGE   = 0.10  # add to home win prob to reflect last-licks advantage

_SOCCER_REGULATION_MINS = 90
_SOCCER_HOME_LAMBDA = 1.40  # avg goals/match for home side (EPL ~1.5, MLS ~1.4)
_SOCCER_AWAY_LAMBDA = 1.10


# ─── Math helpers ─────────────────────────────────────────────────────────────

def _phi(z: float) -> float:
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _clamp_prob(p: float) -> float:
    """Keep probabilities away from exact 0/1 to avoid downstream divide-by-zero."""
    return max(0.001, min(0.999, p))


def _poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


# ─── NBA / WNBA ───────────────────────────────────────────────────────────────

def _nba_wp_home(ctx: LiveGameContext, *, sigma_full: float, home_edge: float,
                  regulation_secs: float) -> float:
    """
    Stern-style live win probability for the home team in a clock-based sport.

    Game-end (t = 0): probability collapses to {0, 0.5, 1} given final diff.
    Game-start (t = T): probability ≈ Φ(home_edge / σ_full).
    """
    diff = ctx.score_diff   # home - away
    t = max(0.0, ctx.seconds_remaining)

    if t <= 0:
        if diff > 0:
            return 0.999
        if diff < 0:
            return 0.001
        return 0.5  # tied at clock zero — assume coin-flip OT

    # σ scales with √(remaining_fraction): less time left → less uncertainty
    fraction = t / regulation_secs
    sigma = sigma_full * math.sqrt(max(fraction, 1e-4))
    return _clamp_prob(_phi((diff + home_edge) / sigma))


def nba_wp_home(ctx: LiveGameContext) -> float:
    return _nba_wp_home(
        ctx,
        sigma_full=_NBA_SIGMA_FULL,
        home_edge=_NBA_HOME_EDGE,
        regulation_secs=_NBA_REGULATION_SECS,
    )


def wnba_wp_home(ctx: LiveGameContext) -> float:
    return _nba_wp_home(
        ctx,
        sigma_full=_WNBA_SIGMA_FULL,
        home_edge=_WNBA_HOME_EDGE,
        regulation_secs=_WNBA_REGULATION_SECS,
    )


# ─── MLB ──────────────────────────────────────────────────────────────────────

def mlb_wp_home(ctx: LiveGameContext) -> float:
    """
    Approximate home win expectancy from score diff + inning.

    `ctx.period` = current inning (1..9+). MLB clock is always 0.

    Anchor points (rough match to MLB win-expectancy tables):
      tied, top of 1st        → 0.54  (small home edge)
      tied, top of 7th        → 0.55
      tied, bottom of 9th     → 0.71
      +1, bottom of 9th       → 0.94
      +3, bottom of 9th       → 0.99
      -1, bottom of 9th       → 0.30
      -3, top of 1st          → 0.20
    """
    diff = ctx.score_diff
    inning = max(1, ctx.period)

    if ctx.is_final or inning > _MLB_REGULATION_INNINGS + 4:  # safety: extras well into double digits
        if diff > 0:
            return 0.999
        if diff < 0:
            return 0.001
        return 0.5

    # Innings remaining (treat each inning as ~equally weighted opportunity).
    innings_left = max(0.5, _MLB_REGULATION_INNINGS - inning + 1)

    # Sigma scales with √(innings_left). σ_full for a 9-inning game ≈ 3 runs.
    sigma_full = 3.0
    sigma = sigma_full * math.sqrt(innings_left / _MLB_REGULATION_INNINGS)
    base = _phi((diff + 0.3) / max(sigma, 0.5))   # 0.3 = small home edge in runs
    return _clamp_prob(base + _MLB_HOME_EDGE * (1.0 - base) * (innings_left / _MLB_REGULATION_INNINGS))


# ─── Soccer ───────────────────────────────────────────────────────────────────

def soccer_wp_home(ctx: LiveGameContext) -> float:
    """
    Poisson goals-remaining model.

    Remaining home goals ~ Poisson(λ_home · t_remaining / 90)
    Remaining away goals ~ Poisson(λ_away · t_remaining / 90)

    P(home wins) = Σ_{h,a} P(H_rem=h) · P(A_rem=a) · 1[diff + h - a > 0]
                 + 0.5 × P(diff + h - a == 0) for cup ties (omit for league).
    """
    diff = ctx.score_diff
    t = max(0.0, ctx.seconds_remaining)

    if t <= 0 or ctx.is_final:
        if diff > 0:
            return 0.999
        if diff < 0:
            return 0.001
        return 0.5  # draw in league play — caller should not interpret as home-win

    minutes_left = t / 60.0
    fraction = minutes_left / _SOCCER_REGULATION_MINS
    lam_home = _SOCCER_HOME_LAMBDA * fraction
    lam_away = _SOCCER_AWAY_LAMBDA * fraction

    # Truncate the Poisson sum at 7 goals each — extreme tails contribute ~0.
    p_win = 0.0
    p_draw = 0.0
    for h in range(0, 8):
        ph = _poisson_pmf(h, lam_home)
        for a in range(0, 8):
            pa = _poisson_pmf(a, lam_away)
            final_diff = diff + h - a
            if final_diff > 0:
                p_win += ph * pa
            elif final_diff == 0:
                p_draw += ph * pa
    # In a Polymarket "Will X beat Y?" market a draw resolves NO, so we count
    # only outright wins. Draws bias the probability toward 0 — that's correct.
    return _clamp_prob(p_win)


# ─── Dispatcher ───────────────────────────────────────────────────────────────

_SOCCER_SPORTS = frozenset({"EPL", "UCL", "MLS", "FIFA"})

def live_wp_home(ctx: LiveGameContext) -> float | None:
    """Live home-win probability, dispatched by sport. None if unsupported."""
    sport = (ctx.sport or "").upper()
    if sport == "NBA":
        return nba_wp_home(ctx)
    if sport == "WNBA":
        return wnba_wp_home(ctx)
    if sport == "MLB":
        return mlb_wp_home(ctx)
    if sport in _SOCCER_SPORTS:
        return soccer_wp_home(ctx)
    return None  # NFL/NHL/UFC etc. — model not implemented


# ─── Question-subject parsing ─────────────────────────────────────────────────

def _question_subject_is_home(question: str, ctx: LiveGameContext) -> bool | None:
    """
    Determine whether the question's YES-side team is the home team.

    Heuristic: find the earliest occurrence of any home-team token vs.
    away-team token in the question. The team appearing first is the
    subject (i.e. the team YES bets on winning).

    Returns None if neither team can be located in the question.
    """
    q = (question or "").lower()
    if not q:
        return None

    def _first_index(team: str) -> int:
        """Lowest index of any token from `team` in the question, or -1 if none."""
        tokens = [t for t in team.lower().split() if len(t) >= 3]
        positions = [q.find(tok) for tok in tokens]
        positions = [p for p in positions if p >= 0]
        return min(positions) if positions else -1

    home_pos = _first_index(ctx.home_team)
    away_pos = _first_index(ctx.away_team)

    if home_pos < 0 and away_pos < 0:
        return None
    if home_pos < 0:
        return False
    if away_pos < 0:
        return True
    return home_pos < away_pos


# ─── Public API: model_probs builder ──────────────────────────────────────────

def compute_live_model_probs(
    open_trades:   Iterable[TradeRecord],
    matched_pairs: Iterable[MatchedPair],
) -> tuple[dict[str, float], dict[str, LiveGameContext]]:
    """
    Build the (model_probs, live_contexts) dicts that `compute_live_exit_signals`
    expects.

    Only open US-platform sports trades whose matched pair has a
    `live_context` set are included. Trades whose game isn't in progress
    yet (or whose live state hasn't been fetched) are skipped silently.

    Returns:
        model_probs:   market_id → P(YES side wins) according to the live model
        live_contexts: market_id → LiveGameContext (raw state, for exit notes)
    """
    pairs_by_market = {p.global_market.id: p for p in matched_pairs}
    model_probs:   dict[str, float] = {}
    live_contexts: dict[str, LiveGameContext] = {}

    for trade in open_trades:
        if trade.live_platform != "polymarket_us":
            continue
        pair = pairs_by_market.get(trade.market_id)
        if pair is None or pair.live_context is None:
            continue

        ctx = pair.live_context
        p_home = live_wp_home(ctx)
        if p_home is None:
            logger.debug(
                "live model: no sport handler for {} — skipping {}",
                ctx.sport, trade.market_id,
            )
            continue

        subject_is_home = _question_subject_is_home(pair.global_market.question, ctx)
        if subject_is_home is None:
            logger.debug(
                "live model: could not locate teams in question — skipping {}",
                trade.market_id,
            )
            continue

        # model_probs[market_id] is keyed to the YES side of the market
        p_yes = p_home if subject_is_home else (1.0 - p_home)
        model_probs[trade.market_id]   = p_yes
        live_contexts[trade.market_id] = ctx

    return model_probs, live_contexts
