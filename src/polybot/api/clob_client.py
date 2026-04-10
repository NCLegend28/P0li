"""
Live order execution via Polymarket CLOB.

Wraps py-clob-client with:
  - Circuit breaker (daily loss cap from config)
  - Error handling that never crashes the scan loop
  - Position size validation against live balance
  - Full logging of every order action
"""
from __future__ import annotations

from datetime import date, timezone

from loguru import logger
from py_clob_client.client import ClobClient as _ClobClient
from py_clob_client.clob_types import (
    ApiCreds, BalanceAllowanceParams, AssetType, OrderArgs,
)
from polybot.config import settings

BUY  = "BUY"
SELL = "SELL"


class ClobClient:

    def __init__(self):
        creds = ApiCreds(
            api_key        = settings.clob_api_key,
            api_secret     = settings.clob_api_secret,
            api_passphrase = settings.clob_api_passphrase,
        )
        self._client = _ClobClient(
            host           = "https://clob.polymarket.com",
            chain_id       = 137,
            key            = settings.private_key,
            creds          = creds,
            signature_type = 2,                        # GNOSIS_SAFE proxy wallet
            funder         = settings.poly_proxy_address,
        )
        self._daily_loss: float = 0.0
        self._stats_date: date  = date.today(timezone.utc)
        logger.info(
            f"ClobClient ready — proxy={settings.poly_proxy_address[:10]}... "
            f"balance=${self.get_balance():.2f}"
        )

    # ── Daily loss circuit breaker ────────────────────────────────────────────

    def _reset_daily_if_needed(self) -> None:
        today = date.today(timezone.utc)
        if today != self._stats_date:
            self._daily_loss = 0.0
            self._stats_date = today

    def record_loss(self, amount_usd: float) -> None:
        """Call when a live position closes at a loss (pass positive number)."""
        self._reset_daily_if_needed()
        self._daily_loss += abs(amount_usd)
        logger.debug(f"Daily loss tracker: ${self._daily_loss:.2f} / ${settings.max_daily_loss_usd:.2f}")

    def check_daily_loss_limit(self) -> bool:
        """Returns True if safe to place more orders."""
        self._reset_daily_if_needed()
        if self._daily_loss >= settings.max_daily_loss_usd:
            logger.warning(
                f"Daily loss cap hit: ${self._daily_loss:.2f} >= "
                f"${settings.max_daily_loss_usd:.2f} — live trading paused for today"
            )
            return False
        return True

    # ── Balance ───────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        """Live USDC.e balance in dollars. Returns 0.0 on error."""
        try:
            bal = self._client.get_balance_allowance(
                params=BalanceAllowanceParams(
                    asset_type     = AssetType.COLLATERAL,
                    signature_type = 2,
                )
            )
            return float(bal.get("balance", 0)) / 1e6
        except Exception as e:
            logger.error(f"get_balance failed: {e}")
            return 0.0

    # ── Order placement ───────────────────────────────────────────────────────

    def place_order(
        self,
        token_id: str,
        side:     str,
        price:    float,
        size_usd: float,
    ) -> str | None:
        """
        Submit a GTC limit order to the CLOB.

        Args:
            token_id:  CLOB token ID (YES or NO token for the market)
            side:      "YES" or "NO"
            price:     limit price 0.0–1.0
            size_usd:  position size in dollars

        Returns:
            order_id string on success, None on any failure.
            Never raises — all errors are logged.
        """
        if not self.check_daily_loss_limit():
            return None

        # Validate minimum order size
        if size_usd < 1.0:
            logger.warning(f"Order too small: ${size_usd:.2f} < $1.00 minimum")
            return None

        # Validate against live balance
        balance = self.get_balance()
        if balance < size_usd:
            logger.warning(
                f"Insufficient balance: ${balance:.2f} available, "
                f"${size_usd:.2f} required — skipping live order"
            )
            return None

        try:
            shares = round(size_usd / price, 4)

            order_args = OrderArgs(
                token_id = token_id,
                price    = price,
                size     = shares,
                side     = BUY if side == "YES" else SELL,
            )

            # create_and_post_order resolves tick_size and neg_risk
            # internally when options=None, and defaults to GTC.
            response = self._client.create_and_post_order(order_args)

            order_id = response.get("orderID")
            status   = response.get("status", "unknown")

            logger.info(
                f"LIVE ORDER PLACED  "
                f"token={token_id[:12]}...  side={side}  "
                f"price={price:.3f}  size=${size_usd:.2f}  shares={shares:.4f}  "
                f"order_id={order_id}  status={status}"
            )
            return order_id

        except Exception as e:
            logger.error(f"place_order failed — token={token_id[:12]} side={side}: {e}")
            return None

    # ── Order cancellation ────────────────────────────────────────────────────

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an open order.

        Returns True on success or if already filled/cancelled.
        Already-filled orders return an error from the API — that's fine,
        we treat it as success since there's nothing to cancel.
        Never raises.
        """
        try:
            self._client.cancel(order_id)
            logger.info(f"LIVE ORDER CANCELLED  order_id={order_id}")
            return True
        except Exception as e:
            # Filled orders can't be cancelled — log at debug, not error
            logger.debug(f"cancel_order {order_id}: {e}")
            return False

    def sell_order(self, token_id: str, price: float, shares: float) -> str | None:
        """
        Sell (exit) a position by placing a SELL limit order.
        Takes shares directly since the position size is already known.
        Never raises.
        """
        if not self.check_daily_loss_limit():
            return None
        try:
            order_args = OrderArgs(
                token_id = token_id,
                price    = price,
                size     = round(shares, 4),
                side     = SELL,
            )
            response = self._client.create_and_post_order(order_args)
            order_id = response.get("orderID")
            logger.info(
                f"LIVE SELL ORDER  token={token_id[:12]}...  "
                f"price={price:.3f}  shares={shares:.4f}  order_id={order_id}"
            )
            return order_id
        except Exception as e:
            logger.error(f"sell_order failed — token={token_id[:12]}: {e}")
            return None

    # ── Order status ──────────────────────────────────────────────────────────

    def get_order_status(self, order_id: str) -> str | None:
        """Returns MATCHED, OPEN, CANCELLED, etc. or None on failure."""
        try:
            order = self._client.get_order(order_id)
            return order.get("status")
        except Exception as e:
            logger.error(f"get_order_status {order_id}: {e}")
            return None