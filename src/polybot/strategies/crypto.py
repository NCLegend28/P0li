"""
Crypto strategy — log-normal price model.

Two market types on Polymarket:
  1. Bracket markets  — "Will BTC be between $90k-$95k on March 31?"
     Model: log-normal distribution, P([L,H]) = Φ(d_hi) - Φ(d_lo)

  2. Up/Down markets  — "Will BTC be higher than $92k at 3pm?"
     Model: drift=0 (EMH), σ from recent 15-minute returns,
     P(close > open) from log-normal CDF.

The analogy: the market crowd bets which price bucket the coin lands in.
CoinGecko gives us the tape + vol. We bet when the crowd's implied
probability diverges from the log-normal estimate by more than the
edge threshold.

Usage:
    from polybot.strategies.crypto import evaluate_crypto_markets
    opps = evaluate_crypto_markets(markets, coin_data, min_edge=0.10)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from loguru import logger

from polybot.api.coingecko import CoinData, CoinGeckoClient
from polybot.models import Market, Opportunity, Side


# ─── Question parser ──────────────────────────────────────────────────────────

@dataclass
class CryptoQuestion:
    coin_id:       str    # CoinGecko ID e.g. "bitcoin"
    symbol:        str    # e.g. "BTC"
    lo:            float  # lower price bound (0 = no lower bound)
    hi:            float  # upper price bound (1e12 = no upper bound)
    market_type:   str    # "bracket" | "above" | "below" | "updown"


_PRICE_MULTIPLIERS = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def _parse_price(s: str) -> float:
    """Parse price strings like '$90k', '95,000', '$1.2M'."""
    s = s.replace(",", "").replace("$", "").strip().lower()
    mul = 1.0
    for suffix, factor in _PRICE_MULTIPLIERS.items():
        if s.endswith(suffix):
            s = s[:-1]
            mul = factor
            break
    try:
        return float(s) * mul
    except ValueError:
        return 0.0


# "between $90k and $95k" or "between 90000-95000"
_BETWEEN_RE = re.compile(
    r'between\s+\$?([\d,.]+[kmb]?)\s*(?:and|-|–)\s*\$?([\d,.]+[kmb]?)',
    re.IGNORECASE,
)
# "above $95k" / "higher than $95k" / "over $95k"
_ABOVE_RE = re.compile(
    r'(?:above|higher than|over)\s+\$?([\d,.]+[kmb]?)',
    re.IGNORECASE,
)
# "below $90k" / "lower than $90k" / "under $90k"
_BELOW_RE = re.compile(
    r'(?:below|lower than|under)\s+\$?([\d,.]+[kmb]?)',
    re.IGNORECASE,
)
# "up or down" / "higher or lower" — binary directional
_UPDOWN_RE = re.compile(r'\b(?:up or down|higher or lower|go up|go down)\b', re.IGNORECASE)


def parse_question(question: str, coin_id: str, symbol: str) -> CryptoQuestion | None:
    """Parse a Polymarket crypto question into a CryptoQuestion."""

    m = _BETWEEN_RE.search(question)
    if m:
        lo = _parse_price(m.group(1))
        hi = _parse_price(m.group(2))
        if lo > 0 and hi > lo:
            return CryptoQuestion(coin_id=coin_id, symbol=symbol, lo=lo, hi=hi, market_type="bracket")

    m = _ABOVE_RE.search(question)
    if m:
        lo = _parse_price(m.group(1))
        if lo > 0:
            return CryptoQuestion(coin_id=coin_id, symbol=symbol, lo=lo, hi=1e12, market_type="above")

    m = _BELOW_RE.search(question)
    if m:
        hi = _parse_price(m.group(1))
        if hi > 0:
            return CryptoQuestion(coin_id=coin_id, symbol=symbol, lo=0.0, hi=hi, market_type="below")

    if _UPDOWN_RE.search(question):
        return CryptoQuestion(coin_id=coin_id, symbol=symbol, lo=0.0, hi=1e12, market_type="updown")

    return None


# ─── Probability models ───────────────────────────────────────────────────────

def _phi(x: float) -> float:
    """Logistic approximation of the standard normal CDF (Φ)."""
    x = max(-50.0, min(50.0, x))
    return 1.0 / (1.0 + math.exp(-1.7 * x))


def lognormal_bracket_prob(
    spot:          float,
    lo:            float,
    hi:            float,
    sigma_daily:   float,
    horizon_hours: float,
) -> float:
    """
    P(price ∈ [lo, hi] at horizon) under log-normal model.

    spot          current spot price
    lo, hi        bracket bounds (use 0 / 1e12 for open-ended)
    sigma_daily   30-day rolling daily vol (e.g. 0.035 for 3.5%)
    horizon_hours hours until market closes
    """
    if horizon_hours <= 0 or sigma_daily <= 0:
        return 0.5

    t = horizon_hours / 24.0
    sigma_t = sigma_daily * math.sqrt(t)
    mu_t = -0.5 * sigma_t ** 2  # risk-neutral drift

    d_lo = (math.log(lo / spot) - mu_t) / sigma_t if lo > 0 else -50.0
    d_hi = (math.log(hi / spot) - mu_t) / sigma_t if hi < 1e9 else 50.0

    prob = _phi(d_hi) - _phi(d_lo)
    return round(max(0.01, min(0.99, prob)), 4)


def updown_prob(spot: float, hourly_ohlc: list, horizon_hours: float) -> float:
    """
    P(price goes up) for a binary Up/Down market.
    Uses drift=0 and σ from recent 15-min equivalent returns (hourly OHLC close-to-close).
    """
    closes = [b.close for b in hourly_ohlc[-24:] if b.close > 0]
    if len(closes) < 2:
        return 0.5  # no signal — skip

    log_returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
    ]
    n = len(log_returns)
    mean_r = sum(log_returns) / n
    variance = sum((r - mean_r) ** 2 for r in log_returns) / max(n - 1, 1)
    sigma = math.sqrt(variance)

    if sigma <= 0:
        return 0.5

    # P(log(P_T/P_0) > 0) with drift=0, at horizon T hours
    t = horizon_hours
    sigma_t = sigma * math.sqrt(t)
    # Under risk-neutral: d = (0 - (-0.5*sigma_t^2)) / sigma_t = 0.5 * sigma_t
    d = 0.5 * sigma_t
    return round(_phi(d), 4)


# ─── Strategy entry point ─────────────────────────────────────────────────────

def evaluate_crypto_markets(
    markets:    list[Market],
    coin_cache: dict[str, CoinData],   # coin_id → CoinData
    min_edge:   float = 0.10,
) -> list[Opportunity]:
    """
    Evaluate all crypto markets against the log-normal model.
    Returns Opportunity list sorted by edge descending.
    """
    opportunities: list[Opportunity] = []

    for market in markets:
        coin_id = CoinGeckoClient.coin_id_from_question(market.question)
        if coin_id is None:
            logger.debug(f"No coin match: {market.question[:65]}")
            continue

        coin = coin_cache.get(coin_id)
        if coin is None:
            logger.debug(f"No coin data: {coin_id}")
            continue

        symbol = coin_id.upper()
        cq = parse_question(market.question, coin_id, symbol)
        if cq is None:
            logger.debug(f"No parse: {market.question[:65]}")
            continue

        sigma_daily = CoinGeckoClient.daily_volatility(coin.daily_ohlc)
        horizon     = market.hours_until_close

        if cq.market_type == "updown":
            model_prob = updown_prob(coin.spot_usd, coin.hourly_ohlc, horizon)
        else:
            model_prob = lognormal_bracket_prob(
                spot=coin.spot_usd,
                lo=cq.lo,
                hi=cq.hi,
                sigma_daily=sigma_daily,
                horizon_hours=horizon,
            )

        market_price = market.yes_price
        edge = model_prob - market_price

        logger.debug(
            f"{market.question[:55]} | "
            f"model={model_prob:.2%} market={market_price:.2%} edge={edge:+.2%}"
        )

        if abs(edge) < min_edge:
            continue

        side = Side.YES if edge > 0 else Side.NO
        entry_price = market_price if side == Side.YES else market.no_price

        opp = Opportunity(
            market=market,
            side=side,
            market_price=entry_price,
            model_probability=model_prob if side == Side.YES else (1 - model_prob),
            edge=abs(edge),
            strategy="crypto_lognormal",
            notes=(
                f"{symbol} spot=${coin.spot_usd:,.0f} | "
                f"σ_daily={sigma_daily:.2%} | "
                f"horizon={horizon:.1f}h | "
                f"bracket=[{cq.lo:,.0f},{cq.hi:,.0f}]"
            ),
        )
        opportunities.append(opp)
        logger.info(
            f"₿ EDGE: {market.question[:50]} | "
            f"{side} @ {entry_price:.3f} | edge={abs(edge):.2%}"
        )

    return sorted(opportunities, key=lambda o: o.edge, reverse=True)
