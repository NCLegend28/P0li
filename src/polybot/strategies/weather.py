"""
Weather trader strategy — v2.

Actual Polymarket question formats (from live API):
  "Will the highest temperature in Dallas be between 80-81°F on March 24?"
  "Will the highest temperature in Hong Kong be 23°C on March 21?"
  "Will the highest temperature in Singapore be 25°C or below on March 24?"
  "Will the highest temperature in New York City be between 66-67°F on March 24?"

The analogy: the market is a crowd betting whether the thermometer
will land in a specific bucket. Open-Meteo is the actual instrument
with known calibration error. We bet when the crowd's implied
probability diverges from the instrument's estimate by more than
our edge threshold.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date

from loguru import logger

from polybot.api.openmeteo import CityForecast, CITY_COORDS
from polybot.models import Market, Opportunity, Side


# ─── Question parser ──────────────────────────────────────────────────────────

@dataclass
class WeatherQuestion:
    city:        str    # matched key into CITY_COORDS
    lo:          float  # bracket low  (always stored in °C)
    hi:          float  # bracket high (always stored in °C)
    unit:        str    # "C" or "F" (original unit in question)
    target_date: str    # ISO date string "YYYY-MM-DD"


_MONTH_MAP = {
    "january": 1,  "february": 2,  "march": 3,     "april": 4,
    "may": 5,      "june": 6,      "july": 7,      "august": 8,
    "september": 9,"october": 10,  "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date(date_str: str) -> str:
    """Convert 'March 24' → '2026-03-24'. Bumps to next year if date has passed."""
    date_str = date_str.strip().lower()
    m = re.search(r'(\w+)\s+(\d{1,2})', date_str)
    if m:
        month = _MONTH_MAP.get(m.group(1))
        if month:
            day   = int(m.group(2))
            today = date.today()
            try:
                candidate = date(today.year, month, day)
                if candidate < today:
                    candidate = date(today.year + 1, month, day)
                return candidate.isoformat()
            except ValueError:
                pass
    return date.today().isoformat()


def _match_city(text: str) -> str | None:
    """
    Return the longest city key that appears as a whole word in text.
    Uses negative lookbehind/lookahead so short keys like 'LA' don't
    fire inside 'Milan' or 'Kuala Lumpur'.
    """
    text_upper = text.upper()
    best, best_len = None, 0
    for key in CITY_COORDS:
        pattern = r'(?<![A-Z])' + re.escape(key) + r'(?![A-Z])'
        if re.search(pattern, text_upper) and len(key) > best_len:
            best, best_len = key, len(key)
    return best


def _to_celsius(value: float, unit: str) -> float:
    return (value - 32) * 5 / 9 if unit == "F" else value


# ─── Regex patterns ───────────────────────────────────────────────────────────

# "between 80-81°F" or "between 80 and 81°C"
_BETWEEN_RE = re.compile(
    r'between\s+(?P<lo>\d+(?:\.\d+)?)\s*[-–]?\s*(?:and\s+)?(?P<hi>\d+(?:\.\d+)?)\s*°?(?P<unit>[CF])\b',
    re.IGNORECASE,
)
# "25°C or below" / "below 70°F"
_BELOW_RE = re.compile(
    r'(?:(?P<val1>\d+(?:\.\d+)?)\s*°?(?P<u1>[CF])\s+or\s+below'
    r'|below\s+(?P<val2>\d+(?:\.\d+)?)\s*°?(?P<u2>[CF]))',
    re.IGNORECASE,
)
# "25°C or above" / "above 70°F"
_ABOVE_RE = re.compile(
    r'(?:(?P<val1>\d+(?:\.\d+)?)\s*°?(?P<u1>[CF])\s+or\s+above'
    r'|above\s+(?P<val2>\d+(?:\.\d+)?)\s*°?(?P<u2>[CF]))',
    re.IGNORECASE,
)
# Exact: "be 23°C on" (single value — treat as ±0.5 bracket)
_EXACT_RE  = re.compile(
    r'\bbe\s+(?P<val>\d+(?:\.\d+)?)\s*°?(?P<unit>[CF])\b',
    re.IGNORECASE,
)
# Date: "on March 24"
_DATE_RE   = re.compile(r'\bon\s+((?:[A-Za-z]+\s+)?\d{1,2})', re.IGNORECASE)


def parse_question(question: str) -> WeatherQuestion | None:
    city = _match_city(question)
    if city is None:
        return None

    dm          = _DATE_RE.search(question)
    target_date = _parse_date(dm.group(1)) if dm else date.today().isoformat()

    m = _BETWEEN_RE.search(question)
    if m:
        unit = m.group("unit").upper()
        return WeatherQuestion(
            city=city,
            lo=_to_celsius(float(m.group("lo")), unit),
            hi=_to_celsius(float(m.group("hi")), unit),
            unit=unit, target_date=target_date,
        )

    m = _BELOW_RE.search(question)
    if m:
        val  = float(m.group("val1") or m.group("val2"))
        unit = (m.group("u1") or m.group("u2")).upper()
        return WeatherQuestion(
            city=city, lo=-999.0,
            hi=_to_celsius(val, unit),
            unit=unit, target_date=target_date,
        )

    m = _ABOVE_RE.search(question)
    if m:
        val  = float(m.group("val1") or m.group("val2"))
        unit = (m.group("u1") or m.group("u2")).upper()
        return WeatherQuestion(
            city=city, lo=_to_celsius(val, unit),
            hi=999.0, unit=unit, target_date=target_date,
        )

    m = _EXACT_RE.search(question)
    if m:
        unit  = m.group("unit").upper()
        val_c = _to_celsius(float(m.group("val")), unit)
        return WeatherQuestion(
            city=city, lo=val_c - 0.5, hi=val_c + 0.5,
            unit=unit, target_date=target_date,
        )

    return None


# ─── Probability model ────────────────────────────────────────────────────────

def estimate_probability(wq: WeatherQuestion, forecast: CityForecast) -> float:
    """
    P(bracket resolves YES) via Normal CDF logistic approximation.

    mu    = Open-Meteo daily high (°C)
    sigma = 1.8°C  (24-48hr NWP MAE for daily max temperature)
    """
    mu, sigma = forecast.high_temp_c, 1.8

    def phi(x: float) -> float:
        x = max(-50.0, min(50.0, x))   # clamp to prevent exp overflow
        return 1.0 / (1.0 + math.exp(-1.7 * x))

    p = phi((wq.hi - mu) / sigma) - phi((wq.lo - mu) / sigma)
    return round(max(0.01, min(0.99, p)), 4)


# ─── Strategy entry point ─────────────────────────────────────────────────────

def evaluate_weather_markets(
    markets:   list[Market],
    forecasts: dict[str, CityForecast],
    min_edge:  float = 0.08,
) -> list[Opportunity]:

    opportunities: list[Opportunity] = []

    for market in markets:
        wq = parse_question(market.question)
        if wq is None:
            logger.debug(f"No parse: {market.question[:65]}")
            continue

        forecast = forecasts.get(wq.city)
        if forecast is None:
            logger.debug(f"No forecast: {wq.city}")
            continue

        model_prob   = estimate_probability(wq, forecast)
        market_price = market.yes_price
        edge         = model_prob - market_price

        logger.debug(
            f"{market.question[:55]} | "
            f"model={model_prob:.2%} market={market_price:.2%} edge={edge:+.2%}"
        )

        if abs(edge) < min_edge:
            continue

        side        = Side.YES if edge > 0 else Side.NO
        entry_price = market_price if side == Side.YES else market.no_price

        opp = Opportunity(
            market            = market,
            side              = side,
            market_price      = entry_price,
            model_probability = model_prob if side == Side.YES else (1 - model_prob),
            edge              = abs(edge),
            strategy          = "weather_trader",
            notes             = (
                f"{wq.city} high={forecast.high_temp_c:.1f}°C "
                f"({forecast.high_temp_f:.1f}°F) | "
                f"bracket=[{wq.lo:.1f},{wq.hi:.1f}]°C | {wq.target_date}"
            ),
        )
        opportunities.append(opp)
        logger.info(
            f"🌡 EDGE: {market.question[:52]} | "
            f"{side} @ {entry_price:.3f} | edge={abs(edge):.2%}"
        )

    return sorted(opportunities, key=lambda o: o.edge, reverse=True)