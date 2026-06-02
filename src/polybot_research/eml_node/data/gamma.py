"""
Gamma client for resolved markets — the discovery half of the Phase 0 pipeline.

Why we don't reuse polybot.api.gamma.GammaClient directly:
    The live-bot client hardcodes `closed=false` and parses into the live
    `Market` model that strips closed markets in its loop. Rather than mutate
    the production client, we write a research-specific one that fetches CLOSED
    markets and parses into our richer `ResolvedMarket` schema.

Verified Gamma semantics codified here (see vault Phase 0.1 / 0.1.5):
- Base URL: https://gamma-api.polymarket.com
- GET /markets, no auth required
- Verified params that work: closed, limit, offset, order, ascending
- Unknown params are SILENTLY IGNORED — never trust an unverified filter
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import httpx
from loguru import logger

from polybot.utils.retry import async_retry
from polybot_research.eml_node.data.models import ResolvedMarket

GAMMA_BASE = "https://gamma-api.polymarket.com"
_PAGE_SIZE = 100


class ResolvedMarketsClient:
    """
    Async client for paginated retrieval of CLOSED Polymarket markets from Gamma.

    Usage
    -----
        async with ResolvedMarketsClient() as client:
            async for market in client.iter_resolved(max_markets=500):
                ...
    """

    def __init__(self, timeout: float = 15.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=GAMMA_BASE,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

    async def __aenter__(self) -> ResolvedMarketsClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._client.aclose()

    @async_retry(
        max_attempts=3,
        base_delay=2.0,
        exceptions=(httpx.HTTPError, httpx.TimeoutException),
    )
    async def _fetch_page(self, *, offset: int, limit: int) -> list[dict[str, Any]]:
        params = {
            "closed": "true",
            "order": "endDate",
            "ascending": "false",
            "limit": limit,
            "offset": offset,
        }
        logger.debug("Gamma resolved fetch: {}", params)
        response = await self._client.get("/markets", params=params)
        response.raise_for_status()
        return response.json()

    async def iter_resolved(
        self,
        *,
        max_markets: int | None = None,
        page_size: int = _PAGE_SIZE,
        page_delay_seconds: float = 0.1,
    ) -> AsyncIterator[ResolvedMarket]:
        """
        Yield ResolvedMarket objects from Gamma, recent-first by endDate.

        Stops when (a) max_markets are yielded, or (b) Gamma returns a short
        page (end of data). NO client-side filtering happens here — see
        polybot_research.eml_node.data.filter for the audit-ready predicate.
        """
        offset = 0
        yielded = 0
        while True:
            page = await self._fetch_page(offset=offset, limit=page_size)
            if not page:
                logger.info(
                    "Gamma resolved exhausted: {} markets yielded across "
                    "{} page(s)",
                    yielded,
                    offset // page_size,
                )
                return

            for raw in page:
                try:
                    market = ResolvedMarket.model_validate(raw)
                except Exception as exc:
                    logger.warning(
                        "Skipping un-parseable market id={}: {}",
                        raw.get("id", "?"),
                        exc,
                    )
                    continue
                yield market
                yielded += 1
                if max_markets is not None and yielded >= max_markets:
                    logger.info(
                        "Gamma resolved cap reached: {} markets yielded",
                        yielded,
                    )
                    return

            if len(page) < page_size:
                logger.info(
                    "Gamma resolved exhausted (short page): {} markets yielded",
                    yielded,
                )
                return

            offset += page_size
            await asyncio.sleep(page_delay_seconds)

    async def fetch_resolved(
        self,
        *,
        max_markets: int = 500,
    ) -> list[ResolvedMarket]:
        """Convenience: collect iter_resolved() into a list."""
        return [m async for m in self.iter_resolved(max_markets=max_markets)]
