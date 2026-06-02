"""Regression tests for trade-log replay & balance reconstruction."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from polybot.models import Side, TradeRecord, TradeStatus
from polybot.trading import engine as engine_mod


def _open_record(opp_id: str, entry: float, size: float = 10.0) -> dict:
    rec = TradeRecord(
        opportunity_id=opp_id,
        market_id=f"mkt-{opp_id}",
        question=f"Q? {opp_id}",
        side=Side.YES,
        entry_price=entry,
        size_usd=size,
        shares=size / entry,
        status=TradeStatus.OPEN,
    )
    return json.loads(rec.model_dump_json())


def _close_record(open_dict: dict, exit_price: float) -> dict:
    closed = dict(open_dict)
    closed["status"] = TradeStatus.CLOSED.value
    closed["exit_price"] = exit_price
    closed["closed_at"] = "2026-05-18T00:00:00+00:00"
    return closed


@pytest.fixture
def trade_log(tmp_path: Path, monkeypatch) -> Path:
    """Write a synthetic trades.jsonl mirroring the real-world double-append pattern."""
    log = tmp_path / "trades.jsonl"
    monkeypatch.setattr(engine_mod, "TRADE_LOG_PATH", log)

    # 3 closed (profit, loss, scratch) + 1 still-open
    o1 = _open_record("opp-A", entry=0.40, size=10.0)
    c1 = _close_record(o1, exit_price=0.60)   # +5.00
    o2 = _open_record("opp-B", entry=0.50, size=10.0)
    c2 = _close_record(o2, exit_price=0.30)   # -4.00
    o3 = _open_record("opp-C", entry=0.20, size=10.0)
    c3 = _close_record(o3, exit_price=0.20)   #  0.00
    o4 = _open_record("opp-D", entry=0.25, size=10.0)  # still open

    with log.open("w") as f:
        for rec in (o1, c1, o2, c2, o3, c3, o4):
            f.write(json.dumps(rec) + "\n")

    return log


def test_load_history_dedupes_open_close_pairs(trade_log):
    """Engine must not treat the open-snapshot of a later-closed trade as still open."""
    engine = engine_mod.TradingEngine()

    assert len(engine.positions) == 1, "only opp-D should remain open"
    assert "opp-D" in engine.positions
    assert len(engine.closed_trades) == 3


def test_load_history_balance_excludes_closed_open_capital(trade_log):
    """Balance must reflect realized PnL + only truly-open size_usd."""
    engine = engine_mod.TradingEngine()

    starting = engine._starting_balance()      # 1000.0 default
    expected_pnl = 5.00 + (-4.00) + 0.00       # +1.00
    expected_open_capital = 10.0               # only opp-D
    expected_balance = starting + expected_pnl - expected_open_capital

    assert engine.balance == pytest.approx(expected_balance, abs=0.01)


def test_starting_balance_is_paper_anchor_even_when_live_wired(trade_log):
    """`_starting_balance` must always return the paper anchor — live wallet
    PnL is tracked separately so the paper book stays coherent on the same
    engine when LIVE_TRADING=true is also enabled."""
    engine = engine_mod.TradingEngine()

    class _FakeClob:
        def get_balance(self) -> float: return 20.0

    # Simulate the CLI wiring up a live client (e.g. for sports US)
    from polybot import config as cfg
    cfg.settings.live_trading = True
    try:
        engine.set_clob_client(_FakeClob())
        # Paper anchor must NOT shift to the live wallet's $20.
        assert engine._starting_balance() == cfg.settings.simulated_starting_balance
        # Paper book is unchanged.
        assert engine.balance == pytest.approx(991.0, abs=0.01)
    finally:
        cfg.settings.live_trading = False


def test_load_history_handles_blank_lines(tmp_path, monkeypatch):
    log = tmp_path / "trades.jsonl"
    monkeypatch.setattr(engine_mod, "TRADE_LOG_PATH", log)
    o = _open_record("opp-X", entry=0.4)
    with log.open("w") as f:
        f.write(json.dumps(o) + "\n\n")  # trailing blank line

    engine = engine_mod.TradingEngine()
    assert len(engine.positions) == 1
