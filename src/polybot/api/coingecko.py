"""
CoinGecko API client — free tier, no key required.

Provides:
  - Current spot price for BTC, ETH, SOL, XRP, DOGE, BNB
  - 30-day daily OHLC for rolling volatility calculation
  - 1-day hourly OHLC for short-term vol (Up/Down markets)

Rate limit: 30 calls/min on free tier.
Cache TTL: 60s — prices move fast; don't cache longer.

Usage:
    async with CoinGeckoClient() as cg:
        spot = await cg.get_spot_price("bitcoin")
        ohlc = await cg.get_daily_ohlc("bitcoin", days=30)
        vol  = cg.daily_volatility(ohlc)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

from polybot.utils.retry import async_retry


COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# Supported assets — maps Polymarket question keywords → CoinGecko coin IDs
ASSET_IDS: dict[str, str] = {
    "BTC":      "bitcoin",
    "ETH":      "ethereum",
    "SOL":      "solana",
    "XRP":      "ripple",
    "DOGE":     "dogecoin",
    "BNB":      "binancecoin",
    "BITCOIN":  "bitcoin",
    "ETHEREUM": "ethereum",
    "SOLANA":   "solana",
}


@dataclass
class OHLCBar:
    timestamp: int   # Unix ms
    open:      float
    high:      float
    low:       float
    close:     float


@dataclass
class CoinData:
    coin_id:       str
    symbol:        str
    spot_usd:      float
    fetched_at:    float   # time.time()
    daily_ohlc:    list[OHLCBar]   # last 30 days
    hourly_ohlc:   list[OHLCBar]   # last 24 hours (for short-term vol)


class CoinGeckoClient:
    """
    Async CoinGecko client with 60-second in-process cache per coin.

    Example
    -------
    async with CoinGeckoClient() as cg:
        data = await cg.fetch_coin("bitcoin")
        print(data.spot_usd, cg.daily_volatility(data.daily_ohlc))
    """

    CACHE_TTL = 60.0  # seconds

    def __init__(self, timeout: float = 10.0):
        self._client = httpx.AsyncClient(
            base_url=COINGECKO_BASE,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        self._cache: dict[str, CoinData] = {}

    async def __aenter__(self) -> CoinGeckoClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._client.aclose()

    # ── Internal fetch helpers ─────────────────────────────────────────────────

    @async_retry(max_attempts=3, base_delay=2.0, exceptions=(httpx.HTTPError, httpx.TimeoutException))
    async def _get_spot(self, coin_id: str) -> float:
        resp = await self._client.get(
            "/simple/price",
            params={"ids": coin_id, "vs_currencies": "usd"},
        )
        resp.raise_for_status()
        data = resp.json()
        return float(data[coin_id]["usd"])

    @async_retry(max_attempts=3, base_delay=2.0, exceptions=(httpx.HTTPError, httpx.TimeoutException))
    async def _get_ohlc(self, coin_id: str, days: int) -> list[OHLCBar]:
        """
        CoinGecko /coins/{id}/ohlc — returns [timestamp, open, high, low, close].
        days=1  → hourly candles for past 24h
        days=30 → daily candles for past 30d
        """
        resp = await self._client.get(
            f"/coins/{coin_id}/ohlc",
            params={"vs_currency": "usd", "days": days},
        )
        resp.raise_for_status()
        bars = resp.json()
        return [
            OHLCBar(
                timestamp=int(b[0]),
                open=float(b[1]),
                high=float(b[2]),
                low=float(b[3]),
                close=float(b[4]),
            )
            for b in bars
        ]

    # ── Public API ─────────────────────────────────────────────────────────────

    async def fetch_coin(self, coin_id: str) -> CoinData:
        """
        Fetch spot price + OHLC for a coin. Returns cached data if < 60s old.
        """
        cached = self._cache.get(coin_id)
        if cached and (time.time() - cached.fetched_at) < self.CACHE_TTL:
            logger.debug(f"CoinGecko cache hit: {coin_id}")
            return cached

        logger.debug(f"CoinGecko fetch: {coin_id}")
        spot, daily_ohlc, hourly_ohlc = await _gather_coin_data(self, coin_id)

        data = CoinData(
            coin_id=coin_id,
            symbol=coin_id.upper(),
            spot_usd=spot,
            fetched_at=time.time(),
            daily_ohlc=daily_ohlc,
            hourly_ohlc=hourly_ohlc,
        )
        self._cache[coin_id] = data
        logger.info(f"CoinGecko {coin_id}: spot=${spot:,.2f}")
        return data

    @staticmethod
    def daily_volatility(ohlc: list[OHLCBar]) -> float:
        """
        30-day rolling daily vol — log returns of close prices.
        Returns annualised daily σ (e.g. 0.035 for 3.5% daily vol).
        Falls back to 0.04 if insufficient data.
        """
        import math
        closes = [b.close for b in ohlc if b.close > 0]
        if len(closes) < 2:
            return 0.04  # fallback: 4% daily vol
        log_returns = [
            math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
        ]
        n = len(log_returns)
        mean = sum(log_returns) / n
        variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
        return math.sqrt(variance)

    @staticmethod
    def coin_id_from_question(question: str) -> str | None:
        """
        Extract CoinGecko coin ID from a Polymarket question string.
        Returns None if no supported asset is found.
        """
        q = question.upper()
        for keyword, coin_id in ASSET_IDS.items():
            if keyword in q:
                return coin_id
        return None


async def _gather_coin_data(
    client: CoinGeckoClient,
    coin_id: str,
) -> tuple[float, list[OHLCBar], list[OHLCBar]]:
    """Fetch spot + daily + hourly OHLC concurrently (3 calls)."""
    import asyncio
    spot_task    = asyncio.create_task(client._get_spot(coin_id))
    daily_task   = asyncio.create_task(client._get_ohlc(coin_id, days=30))
    hourly_task  = asyncio.create_task(client._get_ohlc(coin_id, days=1))
    return await asyncio.gather(spot_task, daily_task, hourly_task)
