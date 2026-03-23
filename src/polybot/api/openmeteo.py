"""
Open-Meteo API client — free, no key, global coverage.

We use this for ALL cities (including US) because:
  1. It covers international cities that NOAA doesn't
  2. Single API vs NOAA's two-step (point lookup → forecast)
  3. Returns data in a clean JSON structure

Docs: https://open-meteo.com/en/docs
"""

from __future__ import annotations

import asyncio

import httpx
from loguru import logger
from pydantic import BaseModel

from polybot.utils.retry import async_retry

OPEN_METEO_BASE = "https://api.open-meteo.com/v1"
GEO_BASE        = "https://geocoding-api.open-meteo.com/v1"

# ─── City registry ────────────────────────────────────────────────────────────
# Expand this as you see new cities appearing on Polymarket

CITY_COORDS: dict[str, tuple[float, float]] = {
    # North America
    "NEW YORK":      (40.7128, -74.0060),
    "NEW YORK CITY": (40.7128, -74.0060),
    "NYC":           (40.7128, -74.0060),
    "CHICAGO":       (41.8781, -87.6298),
    "SEATTLE":       (47.6062, -122.3321),
    "ATLANTA":       (33.7490, -84.3880),
    "DALLAS":        (32.7767, -96.7970),
    "MIAMI":         (25.7617, -80.1918),
    "LOS ANGELES":   (34.0522, -118.2437),
    "LA":            (34.0522, -118.2437),
    "HOUSTON":       (29.7604, -95.3698),
    "PHOENIX":       (33.4484, -112.0740),
    "DENVER":        (39.7392, -104.9903),
    "BOSTON":        (42.3601, -71.0589),
    "SAN FRANCISCO": (37.7749, -122.4194),
    "SF":            (37.7749, -122.4194),
    "LAS VEGAS":     (36.1699, -115.1398),
    "MINNEAPOLIS":   (44.9778, -93.2650),
    # Europe
    "LONDON":        (51.5074, -0.1278),
    "PARIS":         (48.8566,  2.3522),
    "BERLIN":        (52.5200, 13.4050),
    "MADRID":        (40.4168, -3.7038),
    "ROME":          (41.9028, 12.4964),
    "AMSTERDAM":     (52.3676,  4.9041),
    "MOSCOW":        (55.7558, 37.6173),
    "ISTANBUL":      (41.0082, 28.9784),
    # Asia
    "TOKYO":         (35.6762, 139.6503),
    "BEIJING":       (39.9042, 116.4074),
    "SHANGHAI":      (31.2304, 121.4737),
    "HONG KONG":     (22.3193, 114.1694),
    "SINGAPORE":     (1.3521,  103.8198),
    "TAIPEI":        (25.0330, 121.5654),
    "SEOUL":         (37.5665, 126.9780),
    "DUBAI":         (25.2048,  55.2708),
    "MUMBAI":        (19.0760,  72.8777),
    # South America
    "BUENOS AIRES":  (-34.6037, -58.3816),
    "SAO PAULO":     (-23.5505, -46.6333),
    # Oceania
    "SYDNEY":        (-33.8688, 151.2093),
    "MELBOURNE":     (-37.8136, 144.9631),
    "WELLINGTON":    (-41.2866, 174.7756),
    # Additional
    "TORONTO":       (43.6532, -79.3832),
    "VANCOUVER":     (49.2827, -123.1207),
    "LUCKNOW":       (26.8467,  80.9462),
    "DELHI":         (28.6139,  77.2090),
    "BANGKOK":       (13.7563, 100.5018),
    "JAKARTA":       (-6.2088, 106.8456),
    "CAIRO":         (30.0444,  31.2357),
    "LAGOS":         (6.5244,   3.3792),
    "NAIROBI":       (-1.2921,  36.8219),
    "ANKARA":        (39.9334,  32.8597),
    "KARACHI":       (24.8607,  67.0011),
    "TEHRAN":        (35.6892,  51.3890),
    "RIYADH":        (24.7136,  46.6753),
    "CASABLANCA":    (33.5731,  -7.5898),
    "ACCRA":         (5.6037,   -0.1870),
    "LIMA":          (-12.0464, -77.0428),
    "BOGOTA":        (4.7110,   -74.0721),
    "SANTIAGO":      (-33.4489, -70.6693),
    "AMSTERDAM":     (52.3676,   4.9041),
    # Cities seen in live Polymarket data
    "MILAN":         (45.4642,   9.1900),
    "KUALA LUMPUR":  (3.1390,  101.6869),
    "OSAKA":         (34.6937, 135.5023),
    "KOLKATA":       (22.5726,  88.3639),
    "LAHORE":        (31.5204,  74.3587),
    "MANILA":        (14.5995, 120.9842),
    "HO CHI MINH":   (10.8231, 106.6297),
    "HANOI":         (21.0278, 105.8342),
    "CASABLANCA":    (33.5731,  -7.5898),
        "ADDIS ABABA":   (9.0320,   38.7469),
    # Cities with active Polymarket weather markets
    "SHENZHEN":      (22.5431, 114.0579),
    "CHONGQING":     (29.5630, 106.5516),
    "CHENGDU":       (30.5728, 104.0668),
    "WUHAN":         (30.5928, 114.3055),
    "WARSAW":        (52.2297,  21.0122),
    "TEL AVIV":      (32.0853,  34.7818),
    "MUNICH":        (48.1351,  11.5820),
    "VIENNA":        (48.2082,  16.3738),
    "BARCELONA":     (41.3851,   2.1734),
    "STOCKHOLM":     (59.3293,  18.0686),
    "ZURICH":        (47.3769,   8.5417),
    "PRAGUE":        (50.0755,  14.4378),
    "BUDAPEST":      (47.4979,  19.0402),
    "BUCHAREST":     (44.4268,  26.1025),
    "ATHENS":        (37.9838,  23.7275),
    "LISBON":        (38.7223,  -9.1393),
    "BRUSSELS":      (50.8503,   4.3517),
    "COPENHAGEN":    (55.6761,  12.5683),
    "OSLO":          (59.9139,  10.7522),
    "HELSINKI":      (60.1699,  24.9384),
    "GUANGZHOU":     (23.1291, 113.2644),
    "TIANJIN":       (39.3434, 117.3616),
    "NANJING":       (32.0603, 118.7969),
    "XI AN":         (34.3416, 108.9398),
    "XIAN":          (34.3416, 108.9398),
    "HANGZHOU":      (30.2741, 120.1551),
    "SUZHOU":        (31.2989, 120.5853),
    "SHENYANG":      (41.8057, 123.4315),
    "HARBIN":        (45.8038, 126.5350),
    "KUALA LUMPUR":  (3.1390,  101.6869),
    "KARACHI":       (24.8607,  67.0011),
    "LAHORE":        (31.5204,  74.3587),
    "ISLAMABAD":     (33.7294,  73.0931),
    "COLOMBO":       (6.9271,   79.8612),
    "KATHMANDU":     (27.7172,  85.3240),
    "DHAKA":         (23.8103,  90.4125),
    "YANGON":        (16.8661,  96.1951),
    "PHNOM PENH":    (11.5564, 104.9282),
    "VIENTIANE":     (17.9757, 102.6331),
    "ULAANBAATAR":   (47.8864, 106.9057),
    "ALMATY":        (43.2220,  76.8512),
    "TASHKENT":      (41.2995,  69.2401),
    "BAKU":          (40.4093,  49.8671),
    "TBILISI":       (41.6938,  44.8015),
    "YEREVAN":       (40.1872,  44.5152),
    "BEIRUT":        (33.8938,  35.5018),
    "AMMAN":         (31.9539,  35.9106),
    "BAGHDAD":       (33.3152,  44.3661),
    "KUWAIT CITY":   (29.3759,  47.9774),
    "MUSCAT":        (23.5880,  58.3829),
    "DOHA":          (25.2854,  51.5310),
    "ABU DHABI":     (24.4539,  54.3773),
    "CASABLANCA":    (33.5731,  -7.5898),
    "TUNIS":         (36.8190,  10.1658),
    "ALGIERS":       (36.7372,   3.0863),
    "TRIPOLI":       (32.9012,  13.1809),
    "KHARTOUM":      (15.5007,  32.5599),
    "KAMPALA":       (0.3476,   32.5825),
    "DAR ES SALAAM": (-6.7924,  39.2083),
    "LUSAKA":        (-15.4166, 28.2833),
    "HARARE":        (-17.8252, 31.0335),
    "JOHANNESBURG":  (-26.2041, 28.0473),
    "CAPE TOWN":     (-33.9249, 18.4241),
    "ACCRA":         (5.6037,   -0.1870),
    "ABUJA":         (9.0765,   7.3986),
    "KINSHASA":      (-4.4419,  15.2663),
    "ANTANANARIVO":  (-18.8792, 47.5079),
    "GUADALAJARA":   (20.6597, -103.3496),
    "MONTERREY":     (25.6866, -100.3161),
    "BOGOTA":        (4.7110,  -74.0721),
    "LIMA":          (-12.0464, -77.0428),
    "QUITO":         (-0.1807,  -78.4678),
    "LA PAZ":        (-16.5000, -68.1193),
    "SANTIAGO":      (-33.4489, -70.6693),
    "MONTEVIDEO":    (-34.9011, -56.1645),
    "ASUNCION":      (-25.2867, -57.6470),
    "CARACAS":       (10.4806,  -66.9036),
    "PANAMA CITY":   (8.9936,  -79.5197),
    "SAN JOSE":      (9.9281,  -84.0907),
    "GUATEMALA CITY":(14.6349, -90.5069),
    "HAVANA":        (23.1136, -82.3666),
    "PORT AU PRINCE":( 18.5944, -72.3074),
    "SANTO DOMINGO": (18.4861, -69.9312),
    "SAN JUAN":      (18.4655, -66.1057),
    "PERTH":         (-31.9505, 115.8605),
    "BRISBANE":      (-27.4698, 153.0251),
    "AUCKLAND":      (-36.8485, 174.7633),
    "SUVA":          (-18.1416, 178.4419),
    "PORT MORESBY":  (-9.4438,  147.1803),
}


class CityForecast(BaseModel):
    city_key:      str
    lat:           float
    lon:           float
    high_temp_c:   float   # Celsius
    low_temp_c:    float
    high_temp_f:   float   # Fahrenheit (converted)
    low_temp_f:    float

    @classmethod
    def from_raw(cls, city_key: str, lat: float, lon: float, raw: dict) -> "CityForecast":
        daily  = raw["daily"]
        high_c = float(daily["temperature_2m_max"][0])
        low_c  = float(daily["temperature_2m_min"][0])
        return cls(
            city_key    = city_key,
            lat         = lat,
            lon         = lon,
            high_temp_c = high_c,
            low_temp_c  = low_c,
            high_temp_f = round(high_c * 9 / 5 + 32, 2),
            low_temp_f  = round(low_c  * 9 / 5 + 32, 2),
        )


class OpenMeteoClient:
    def __init__(self, timeout: float = 20.0):
        self._client = httpx.AsyncClient(
            base_url = OPEN_METEO_BASE,
            timeout  = timeout,
            headers  = {"Accept": "application/json"},
        )

    async def __aenter__(self) -> "OpenMeteoClient":
        return self

    async def __aexit__(self, *_) -> None:
        await self._client.aclose()

    @async_retry(max_attempts=3, base_delay=1.0, exceptions=(httpx.HTTPError, httpx.TimeoutException))
    async def fetch_forecast(self, city_key: str, target_date: str | None = None) -> CityForecast:
        """
        Fetch daily high/low for a city.
        target_date: ISO date string e.g. '2026-03-24', defaults to today.
        """
        key = city_key.upper()
        lat, lon = CITY_COORDS[key]

        params = {
            "latitude":    lat,
            "longitude":   lon,
            "daily":       "temperature_2m_max,temperature_2m_min",
            "temperature_unit": "celsius",
            "timezone":    "auto",
            "forecast_days": 7,
        }

        resp = await self._client.get("/forecast", params=params)
        resp.raise_for_status()
        raw = resp.json()

        if target_date:
            # Find the index matching our target date
            dates = raw["daily"]["time"]
            if target_date in dates:
                try:
                    idx = dates.index(target_date)
                    raw["daily"]["temperature_2m_max"] = [raw["daily"]["temperature_2m_max"][idx]]
                    raw["daily"]["temperature_2m_min"] = [raw["daily"]["temperature_2m_min"][idx]]
                except (ValueError, IndexError) as e:
                    logger.warning(
                        f"Date index lookup failed for {target_date!r}: {e}. "
                        "Falling back to today (index 0)."
                    )
            # Else fall through to today (index 0)

        fc = CityForecast.from_raw(key, lat, lon, raw)
        logger.debug(f"{key}: high={fc.high_temp_c:.1f}°C ({fc.high_temp_f:.1f}°F)")
        return fc

    async def fetch_all_cities(
        self,
        cities: list[str],
        target_date: str | None = None,
    ) -> dict[str, CityForecast]:
        valid = [c.upper() for c in cities if c.upper() in CITY_COORDS]
        for c in cities:
            if c.upper() not in CITY_COORDS:
                logger.warning(f"No coordinates for city: {c}")

        async def _fetch(key: str) -> tuple[str, CityForecast | None]:
            try:
                return key, await self.fetch_forecast(key, target_date)
            except Exception as exc:
                logger.warning(f"Forecast failed for {key}: {exc}")
                return key, None

        pairs = await asyncio.gather(*[_fetch(k) for k in valid])
        return {k: fc for k, fc in pairs if fc is not None}