"""Regression tests for TradingEngine._load_history — duplicate-close immunity."""
from __future__ import annotations

import json
from pathlib import Path

from polybot.models import Side, TradeRecord, TradeStatus
from polybot.trading import engine as engine_module


def _write_log(path: Path, records: list[dict]) -> None:
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _record(opp_id: str, status: str, exit_price: float | None = None, size_usd: float = 10.0) -> dict:
    rec = TradeRecord(
        opportunity_id = opp_id,
        market_id      = f"m-{opp_id}",
        question       = f"Q-{opp_id}",
        side           = Side.NO,
        entry_price    = 0.6,
        size_usd       = size_usd,
        shares         = size_usd / 0.6,
        status         = TradeStatus(status),
        exit_price     = exit_price,
    ).model_dump(mode="json")
    return rec


def test_load_history_dedupes_by_opportunity_id(tmp_path, monkeypatch):
    """A second close for the same opp_id should not double-count P&L or re-open the position."""
    log_path = tmp_path / "trades.jsonl"
    _write_log(log_path, [
        _record("X", "open"),
        _record("X", "closed", exit_price=1.0),    # winner
        _record("X", "closed", exit_price=1.0),    # duplicate close
        _record("Y", "open"),                       # still open
    ])
    monkeypatch.setattr(engine_module, "TRADE_LOG_PATH", log_path)

    engine = engine_module.TradingEngine()

    assert list(engine.positions.keys()) == ["Y"]
    assert len(engine.closed_trades) == 1
    assert engine.closed_trades[0].opportunity_id == "X"


def test_load_history_handles_corrupted_seoul_pattern(tmp_path, monkeypatch):
    """The exact pattern seen in production: open → close → close → close → close (final at 1.0)."""
    log_path = tmp_path / "trades.jsonl"
    _write_log(log_path, [
        _record("seoul", "open"),
        _record("seoul", "closed", exit_price=0.545),
        _record("seoul", "closed", exit_price=0.425),
        _record("seoul", "closed", exit_price=0.425),
        _record("seoul", "closed", exit_price=1.0),
    ])
    monkeypatch.setattr(engine_module, "TRADE_LOG_PATH", log_path)

    engine = engine_module.TradingEngine()

    # Only ONE closed record, and it's the final-resolution one.
    assert len(engine.closed_trades) == 1
    assert engine.closed_trades[0].exit_price == 1.0
    assert "seoul" not in engine.positions
