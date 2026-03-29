"""
Polymarket US API client for sports trading.

Uses the official polymarket-us SDK (Ed25519 auth).
Has NOTHING to do with the global CLOB client (py-clob-client).

Ref: https://docs.polymarket.us/getting-started/quickstart
SDK: https://docs.polymarket.us/api-reference/sdks/python/quickstart

Import firewall: this module NEVER imports py-clob-client or references
the global CLOB infrastructure. The global gamma.py is a separate read-only
data source used by the scanner — it does not touch this module.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from loguru import logger

try:
    from polymarket_us import PolymarketUS, AsyncPolymarketUS
    from polymarket_us import (
        AuthenticationError,
        BadRequestError,
        NotFoundError,
        RateLimitError,
        APITimeoutError,
        APIConnectionError,
    )
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False


def _require_sdk() -> None:
    if not _SDK_AVAILABLE:
        raise ImportError(
            "polymarket-us SDK not installed. Run: uv add polymarket-us"
        )


# ─── Sync client ──────────────────────────────────────────────────────────────

class PolymarketUSClient:
    """
    Wraps the official polymarket-us Python SDK with safety controls.

    Used by the Trader for live sports order execution on the US platform.
    All order placement on the US platform goes through THIS class only.
    """

    def __init__(self, key_id: str, secret_key: str, max_daily_loss: float = 50.0):
        _require_sdk()
        self._client = PolymarketUS(
            key_id=key_id,
            secret_key=secret_key,
            timeout=30.0,
        )
        self._max_daily_loss = max_daily_loss
        self._daily_loss = 0.0
        self._loss_date = date.today()

    # ── Market data (public, no auth) ─────────────────────────────────────────

    def list_events(self, limit: int = 50, active: bool = True) -> dict[str, Any]:
        """Fetch active events (sports games)."""
        return self._client.events.list({"limit": limit, "active": active})

    def get_market(self, slug: str) -> dict[str, Any]:
        """Fetch a single market by slug."""
        return self._client.markets.retrieve_by_slug(slug)

    def get_book(self, slug: str) -> dict[str, Any]:
        """Fetch order book depth for a market."""
        return self._client.markets.book(slug)

    def get_bbo(self, slug: str) -> dict[str, Any]:
        """Fetch best bid/offer for a market."""
        return self._client.markets.bbo(slug)

    def search_markets(self, query: str) -> dict[str, Any]:
        """Search markets by keyword."""
        return self._client.search.query({"query": query})

    def list_sports(self) -> dict[str, Any]:
        """Fetch available sports categories."""
        return self._client.sports.list()

    # ── Account (authenticated) ───────────────────────────────────────────────

    def get_balance(self) -> dict[str, Any]:
        """Fetch account balances."""
        return self._client.account.balances()

    def get_positions(self) -> dict[str, Any]:
        """Fetch open positions."""
        return self._client.portfolio.positions()

    def get_activities(self) -> dict[str, Any]:
        """Fetch recent account activity."""
        return self._client.portfolio.activities()

    # ── Orders (authenticated) ────────────────────────────────────────────────

    def place_order(
        self,
        market_slug: str,
        side: str,          # "YES" or "NO"
        price: float,       # 0.01 – 0.99
        quantity: int,      # number of contracts
        tif: str = "GTC",
    ) -> dict[str, Any] | None:
        """
        Place a limit order on the US platform.

        Returns the order dict on success, None on any failure.
        Unlike the global ClobClient (which silently returns None), this client
        logs the SPECIFIC error type so failures are always diagnosable.
        """
        # Reset daily loss counter at midnight
        if self._loss_date != date.today():
            self._daily_loss = 0.0
            self._loss_date = date.today()

        if self._daily_loss >= self._max_daily_loss:
            logger.warning(
                "US daily loss cap (${:.2f}) hit — skipping order on {}",
                self._max_daily_loss, market_slug,
            )
            return None

        intent = (
            "ORDER_INTENT_BUY_LONG" if side == "YES"
            else "ORDER_INTENT_BUY_SHORT"
        )

        tif_map = {
            "GTC": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
            "IOC": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
            "FOK": "TIME_IN_FORCE_FILL_OR_KILL",
        }

        try:
            order = self._client.orders.create({
                "marketSlug": market_slug,
                "intent": intent,
                "type": "ORDER_TYPE_LIMIT",
                "price": {"value": str(price), "currency": "USD"},
                "quantity": quantity,
                "tif": tif_map.get(tif, "TIME_IN_FORCE_GOOD_TILL_CANCEL"),
            })
            logger.info(
                "US ORDER PLACED: {} {} @ ${:.2f} x{} → id={}",
                side, market_slug, price, quantity,
                order.get("id", "???"),
            )
            return order

        except AuthenticationError as e:
            logger.error("US auth error placing order on {}: {}", market_slug, e.message)
        except BadRequestError as e:
            logger.error("US bad request on {}: {}", market_slug, e.message)
        except RateLimitError as e:
            logger.error("US rate limited on {}: {}", market_slug, e.message)
        except NotFoundError as e:
            logger.error("US market not found '{}': {}", market_slug, e.message)
        except APITimeoutError:
            logger.error("US timeout placing order on {}", market_slug)
        except APIConnectionError as e:
            logger.error("US connection error on {}: {}", market_slug, e.message)
        except Exception as e:
            logger.error("US unexpected error placing order on {}: {}", market_slug, e)

        return None

    def cancel_order(self, order_id: str) -> dict[str, Any] | None:
        """Cancel an open order by ID."""
        try:
            return self._client.orders.cancel(order_id)
        except Exception as e:
            logger.error("US cancel order {} failed: {}", order_id, e)
            return None

    def cancel_all(self) -> dict[str, Any] | None:
        """Cancel all open orders."""
        try:
            return self._client.orders.cancel_all()
        except Exception as e:
            logger.error("US cancel_all failed: {}", e)
            return None

    def list_orders(self) -> dict[str, Any]:
        """Fetch all open orders."""
        return self._client.orders.list()

    def preview_order(self, params: dict[str, Any]) -> dict[str, Any] | None:
        """Preview order impact without placing it."""
        try:
            return self._client.orders.preview(params)
        except Exception as e:
            logger.error("US order preview failed: {}", e)
            return None

    def close_position(self, market_slug: str) -> dict[str, Any] | None:
        """Close an entire position in a market."""
        try:
            return self._client.orders.close_position(market_slug)
        except Exception as e:
            logger.error("US close position '{}' failed: {}", market_slug, e)
            return None

    def record_loss(self, amount: float) -> None:
        """Track realised losses against the daily circuit breaker."""
        if amount > 0:
            self._daily_loss += amount

    def close(self) -> None:
        """Clean up the SDK client."""
        self._client.close()


# ─── Async client ─────────────────────────────────────────────────────────────

class AsyncPolymarketUSClient:
    """
    Async wrapper for use inside the LangGraph sports scanner pipeline.

    Used only for READ operations in the scanner (price discovery).
    ORDER PLACEMENT is handled by the sync PolymarketUSClient in the Trader.

    Ref: https://docs.polymarket.us/api-reference/sdks/python/quickstart#async-usage
    """

    def __init__(self, key_id: str, secret_key: str):
        _require_sdk()
        self._client = AsyncPolymarketUS(
            key_id=key_id,
            secret_key=secret_key,
        )

    async def list_events(self, limit: int = 50, active: bool = True) -> dict[str, Any]:
        return await self._client.events.list({"limit": limit, "active": active})

    async def get_book(self, slug: str) -> dict[str, Any]:
        return await self._client.markets.book(slug)

    async def get_bbo(self, slug: str) -> dict[str, Any]:
        return await self._client.markets.bbo(slug)

    async def search(self, query: str) -> dict[str, Any]:
        return await self._client.search.query({"query": query})

    async def list_markets(self, limit: int = 100) -> dict[str, Any]:
        return await self._client.markets.list({"limit": limit})

    async def close(self) -> None:
        await self._client.close()
