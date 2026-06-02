"""Unit tests for the weather trading strategy."""
from __future__ import annotations

import pytest

from polybot.api.openmeteo import CityForecast
from polybot.strategies.weather import WeatherQuestion, estimate_probability, parse_question


def _make_forecast(high_c: float) -> CityForecast:
    return CityForecast(
        city_key="DALLAS",
        lat=32.77,
        lon=-96.79,
        high_temp_c=high_c,
        low_temp_c=high_c - 10,
        high_temp_f=round(high_c * 9 / 5 + 32, 2),
        low_temp_f=round((high_c - 10) * 9 / 5 + 32, 2),
    )


class TestParseQuestion:
    def test_between_fahrenheit(self):
        wq = parse_question(
            "Will the highest temperature in Dallas be between 80-81°F on March 24?"
        )
        assert wq is not None
        assert wq.city == "DALLAS"
        assert wq.unit == "F"
        # 80°F ≈ 26.67°C, 81°F ≈ 27.22°C
        assert abs(wq.lo - 26.67) < 0.1
        assert abs(wq.hi - 27.22) < 0.1

    def test_exact_celsius(self):
        wq = parse_question(
            "Will the highest temperature in Hong Kong be 23°C on March 21?"
        )
        assert wq is not None
        assert wq.city == "HONG KONG"
        assert wq.unit == "C"
        assert wq.lo == pytest.approx(22.5, abs=0.01)
        assert wq.hi == pytest.approx(23.5, abs=0.01)

    def test_below_celsius(self):
        wq = parse_question(
            "Will the highest temperature in Singapore be 25°C or below on March 24?"
        )
        assert wq is not None
        assert wq.city == "SINGAPORE"
        assert wq.lo == -999.0
        assert wq.hi == pytest.approx(25.0, abs=0.01)

    def test_above_fahrenheit(self):
        wq = parse_question(
            "Will the highest temperature in Miami be above 90°F on March 24?"
        )
        assert wq is not None
        assert wq.city == "MIAMI"
        assert wq.hi == 999.0

    def test_unknown_city_returns_none(self):
        wq = parse_question("Will it be hot in Atlantis on March 24?")
        assert wq is None

    def test_no_date_defaults_to_today(self):
        from datetime import date
        wq = parse_question("Will the highest temperature in Tokyo be 20°C?")
        assert wq is not None
        assert wq.target_date == date.today().isoformat()


class TestEstimateProbability:
    def test_forecast_inside_bracket_high_probability(self):
        wq = WeatherQuestion(
            city="DALLAS", lo=25.0, hi=28.0, unit="C", target_date="2026-03-24"
        )
        fc = _make_forecast(26.5)  # centre of bracket
        p = estimate_probability(wq, fc)
        assert p > 0.5

    def test_forecast_far_below_bracket_low_probability(self):
        wq = WeatherQuestion(
            city="DALLAS", lo=25.0, hi=28.0, unit="C", target_date="2026-03-24"
        )
        fc = _make_forecast(5.0)  # well below bracket
        p = estimate_probability(wq, fc)
        assert p < 0.1

    def test_forecast_far_above_bracket_low_probability(self):
        wq = WeatherQuestion(
            city="DALLAS", lo=25.0, hi=28.0, unit="C", target_date="2026-03-24"
        )
        fc = _make_forecast(45.0)  # well above bracket
        p = estimate_probability(wq, fc)
        assert p < 0.1

    def test_probability_clamped_between_0_01_and_0_99(self):
        wq = WeatherQuestion(
            city="DALLAS", lo=100.0, hi=110.0, unit="C", target_date="2026-03-24"
        )
        fc = _make_forecast(0.0)
        p = estimate_probability(wq, fc)
        assert 0.01 <= p <= 0.99

    def test_above_bracket_is_open_ended(self):
        wq = WeatherQuestion(
            city="DALLAS", lo=25.0, hi=999.0, unit="C", target_date="2026-03-24"
        )
        fc = _make_forecast(30.0)
        p = estimate_probability(wq, fc)
        assert p > 0.5
