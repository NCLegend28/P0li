"""
NOAA Weather API client — free, no key required.

Docs: https://www.weather.gov/documentation/services-web-api
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from loguru import logger

from polybot.utils.retry import async_retry

NOAA_BASE = "https://api.weather.gov"

# lat/lon for cities commonly seen in Polymarket weather markets
CITY_COORDS: dict[str, tuple[float, float]] = {
    "NYC":      (40.7128, -74.0060),
    "NEW YORK": (40.7128, -74.0060),
    "CHICAGO":  (41.8781, -87.6298),
    "SEATTLE":  (47.6062, -122.3321),
    "ATLANTA":  (33.7490, -84.3880),
    "DALLAS":   (32.7767, -96.7970),
    "MIAMI":    (25.7617, -80.1918),
    "LA":       (34.0522, -118.2437),
    "LOS ANGELES": (34.0522, -118.2437),
    "HOUSTON":  (29.7604, -95.3698),
    "PHOENIX":  (33.4484, -112.0740),
    "DENVER":   (39.7392, -104.9903),
    "BOSTON":   (42.3601, -71.0589),
}


@dataclass
class ForecastPeriod:
    name:               str       # "Tonight", "Thursday", etc.
    temperature:        float     # Fahrenheit
    temperature_unit:   str       # "F"
    short_forecast:     str
    is_daytime:         bool


@dataclass
class CityForecast:
    city_key:   str
    lat:        float
    lon:        float
    periods:    list[ForecastPeriod]

    @property
    def current_temp(self) -> float:
        return self.periods[0].temperature if self.periods else 0.0

    @property
    def high_temp(self) -> float:
        daytime = [p for p in self.periods if p.is_daytime]
        return max((p.temperature for p in daytime), default=self.current_temp)

    @property
    def low_temp(self) -> float:
        nighttime = [p for p in self.periods if not p.is_daytime]
        return min((p.temperature for p in nighttime), default=self.current_temp)


class NOAAClient:
    def __init__(self, timeout: float = 20.0):
        self._client = httpx.AsyncClient(
            base_url = NOAA_BASE,
            timeout  = timeout,
            headers  = {
                "User-Agent": "polymarket-bot/0.1 (research project)",
                "Accept":     "application/geo+json",
            },
        )

    async def __aenter__(self) -> NOAAClient:
        return self

    async def __aexit__(self, *_) -> None:
        await self._client.aclose()

    @async_retry(max_attempts=3, base_delay=2.0, exceptions=(httpx.HTTPError, httpx.TimeoutException))
    async def _get_grid_point(self, lat: float, lon: float) -> tuple[str, int, int]:
        """Resolve lat/lon → NOAA grid office + grid coordinates."""
        resp = await self._client.get(f"/points/{lat:.4f},{lon:.4f}")
        resp.raise_for_status()
        props = resp.json()["properties"]
        return props["gridId"], props["gridX"], props["gridY"]

    @async_retry(max_attempts=3, base_delay=2.0, exceptions=(httpx.HTTPError, httpx.TimeoutException))
    async def fetch_forecast(self, city_key: str) -> CityForecast:
        key = city_key.upper()
        lat, lon = CITY_COORDS[key]

        office, gx, gy = await self._get_grid_point(lat, lon)
        logger.debug(f"NOAA grid: {office}/{gx},{gy} for {city_key}")

        resp = await self._client.get(f"/gridpoints/{office}/{gx},{gy}/forecast")
        resp.raise_for_status()

        raw_periods = resp.json()["properties"]["periods"]
        periods = [
            ForecastPeriod(
                name             = p["name"],
                temperature      = float(p["temperature"]),
                temperature_unit = p["temperatureUnit"],
                short_forecast   = p["shortForecast"],
                is_daytime       = p["isDaytime"],
            )
            for p in raw_periods[:8]   # only next 4 days
        ]

        return CityForecast(city_key=key, lat=lat, lon=lon, periods=periods)

    async def fetch_all_cities(self, cities: list[str]) -> dict[str, CityForecast]:
        """Fetch forecasts for multiple cities; skip any that fail."""
        results: dict[str, CityForecast] = {}
        for city in cities:
            key = city.upper()
            if key not in CITY_COORDS:
                logger.warning(f"No coordinates for city: {city}")
                continue
            forecast = await self.fetch_forecast(key)
            results[key] = forecast
            logger.debug(f"{key}: high={forecast.high_temp}°F low={forecast.low_temp}°F")
        return results
