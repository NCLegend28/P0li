"""
The Graph subgraph client for Polymarket CTF Exchange OrderFilledEvent data.

Endpoint (verified 2026-05-10):
    https://gateway.thegraph.com/api/subgraphs/id/7fu2DWYK93ePfzB24c2wrP94S3x4LGHUrQxphhoEypyY

Auth: Bearer token in Authorization header. Key MUST be loaded from .env via
GRAPH_API_KEY — never hardcode or pass on the command line.

Schema (verified, snapshot saved to data/research/eml_node/schema_snapshots/):
    OrderFilledEvent { id, transactionHash, timestamp, blockNumber, orderHash,
                       maker, taker, makerAssetId, takerAssetId,
                       makerAmountFilled, takerAmountFilled, fee,
                       side, price (BigDecimal, pre-computed) }

The pre-computed `price` field eliminates the entire amount-arithmetic /
unit-conversion bug class that the previous Phase 0 plan worried about.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, AsyncIterator

import httpx
from loguru import logger

from polybot.utils.retry import async_retry
from polybot_research.eml_node.data.models import Fill

SUBGRAPH_ID = "7fu2DWYK93ePfzB24c2wrP94S3x4LGHUrQxphhoEypyY"
SUBGRAPH_URL = f"https://gateway.thegraph.com/api/subgraphs/id/{SUBGRAPH_ID}"

_FILLS_FOR_ASSET_QUERY = """
query FillsForAsset(
  $assetId: String!,
  $first: Int!,
  $skip: Int!
) {
  orderFilledEvents(
    where: {
      or: [
        { makerAssetId: $assetId },
        { takerAssetId: $assetId }
      ]
    },
    first: $first,
    skip: $skip,
    orderBy: timestamp,
    orderDirection: asc
  ) {
    id
    transactionHash
    timestamp
    blockNumber
    orderHash
    maker
    taker
    makerAssetId
    takerAssetId
    makerAmountFilled
    takerAmountFilled
    fee
    side
    price
  }
}
"""


def _parse_fill(raw: dict[str, Any]) -> Fill:
    """Coerce subgraph BigInt/BigDecimal scalars (returned as strings) into Python types."""
    return Fill(
        id=str(raw["id"]),
        transaction_hash=str(raw["transactionHash"]),
        timestamp=int(raw["timestamp"]),
        block_number=int(raw["blockNumber"]),
        order_hash=str(raw["orderHash"]),
        maker=str(raw["maker"]),
        taker=str(raw["taker"]),
        maker_asset_id=str(raw["makerAssetId"]),
        taker_asset_id=str(raw["takerAssetId"]),
        maker_amount_filled=int(raw["makerAmountFilled"]),
        taker_amount_filled=int(raw["takerAmountFilled"]),
        price=float(raw["price"]),
        side=str(raw["side"]),
        fee=int(raw["fee"]),
    )


class SubgraphClient:
    """
    Async client for the Polymarket CTF Exchange subgraph.

    Reads GRAPH_API_KEY from the environment. Will raise RuntimeError on
    construction if the key is missing — fail fast rather than getting a
    confusing 401 on first query.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("GRAPH_API_KEY", "")
        if not key:
            raise RuntimeError(
                "GRAPH_API_KEY is not set. Add it to your .env (loaded via "
                "python-dotenv from polybot.config.Settings) or export it."
            )
        # Note: deliberately NOT using base_url. When httpx is given a base_url
        # and POSTs to an empty path it appends a trailing "/", which the Graph
        # gateway 404s on. Pass the absolute URL on every request instead.
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )

    async def __aenter__(self) -> SubgraphClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._client.aclose()

    @async_retry(
        max_attempts=3,
        base_delay=2.0,
        exceptions=(httpx.HTTPError, httpx.TimeoutException),
    )
    async def _post(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = {"query": query, "variables": variables or {}}
        # Use the absolute SUBGRAPH_URL — see __init__ comment on why.
        response = await self._client.post(SUBGRAPH_URL, json=payload)
        response.raise_for_status()
        body = response.json()
        if "errors" in body:
            raise RuntimeError(f"GraphQL errors: {body['errors']}")
        return body.get("data", {})

    async def introspect_type(self, type_name: str) -> dict[str, Any]:
        """Run a GraphQL introspection query for a single type. Used by schema_check."""
        query = (
            "{ __type(name: \"%s\") { "
            "fields { name type { name kind ofType { name kind } } } } }"
        ) % type_name
        return await self._post(query)

    async def list_types(self) -> dict[str, Any]:
        """Run a GraphQL introspection query for all types. Used by schema_check."""
        query = "{ __schema { types { name kind } } }"
        return await self._post(query)

    async def iter_fills_for_asset(
        self,
        asset_id: str,
        *,
        page_size: int = 1000,
        max_pages: int | None = None,
        page_delay_seconds: float = 0.1,
    ) -> AsyncIterator[Fill]:
        """
        Yield all fills for a CTF token asset ID, oldest first.

        Pages with `first` + `skip`. Subgraph has a hard cap of 1000 per page
        and 5000 skip; for high-volume assets this won't be enough — Phase 0.4
        will need to switch to keyset pagination by `timestamp_gt` for those.
        Logged as a TODO inline.
        """
        skip = 0
        page_index = 0
        while True:
            data = await self._post(
                _FILLS_FOR_ASSET_QUERY,
                variables={"assetId": asset_id, "first": page_size, "skip": skip},
            )
            page: list[dict[str, Any]] = data.get("orderFilledEvents", [])
            for raw in page:
                yield _parse_fill(raw)

            if len(page) < page_size:
                logger.debug(
                    "Subgraph fills for asset {}: exhausted at skip={} "
                    "({} pages, last page {} rows)",
                    asset_id[:12],
                    skip,
                    page_index + 1,
                    len(page),
                )
                return

            skip += page_size
            page_index += 1

            # TODO Phase 0.4: when skip > 5000 the subgraph rejects the query;
            # switch to keyset pagination by timestamp_gt for high-volume assets.
            if skip >= 5000:
                logger.warning(
                    "Subgraph skip cap of 5000 reached for asset {}; truncating "
                    "fills. Implement keyset pagination by timestamp_gt to "
                    "complete the history.",
                    asset_id[:12],
                )
                return

            if max_pages is not None and page_index >= max_pages:
                return

            await asyncio.sleep(page_delay_seconds)

    async def fetch_fills_for_asset(
        self,
        asset_id: str,
        *,
        page_size: int = 1000,
        max_pages: int | None = None,
    ) -> list[Fill]:
        """Convenience: collect iter_fills_for_asset into a list."""
        return [
            f
            async for f in self.iter_fills_for_asset(
                asset_id, page_size=page_size, max_pages=max_pages
            )
        ]
