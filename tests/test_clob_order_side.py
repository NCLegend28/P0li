from __future__ import annotations

from typing import Any, cast

from polybot.api.clob_client import BUY, ClobClient


class _FakePyClob:
    def __init__(self):
        self.order_args: Any | None = None

    def get_balance_allowance(self, params):
        return {"balance": 1_000_000_000}

    def create_and_post_order(self, order_args):
        self.order_args = order_args
        return {"orderID": "order-1", "status": "open"}


def _client() -> tuple[ClobClient, _FakePyClob]:
    fake = _FakePyClob()
    client = cast(ClobClient, object.__new__(ClobClient))
    client.__dict__["_client"] = fake
    client._daily_loss = 0.0
    from datetime import datetime, timezone
    client._stats_date = datetime.now(timezone.utc).date()
    return client, fake


def test_buying_yes_token_submits_buy_order():
    client, fake = _client()

    order_id = client.place_order(
        token_id="yes-token",
        side="YES",
        price=0.35,
        size_usd=10.0,
    )

    assert order_id == "order-1"
    assert fake.order_args.token_id == "yes-token"
    assert fake.order_args.side == BUY


def test_buying_no_token_submits_buy_order_not_sell():
    client, fake = _client()

    order_id = client.place_order(
        token_id="no-token",
        side="NO",
        price=0.65,
        size_usd=10.0,
    )

    assert order_id == "order-1"
    assert fake.order_args.token_id == "no-token"
    assert fake.order_args.side == BUY
