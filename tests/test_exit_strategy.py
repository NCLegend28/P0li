"""Unit tests for the exit signal engine."""
from __future__ import annotations

import pytest

from polybot.strategies.exit import ExitReason, compute_exit_signals


class TestComputeExitSignals:
    def test_profit_target_hit(self, open_trade):
        # Entry at 0.35, target = min(0.35 * 1.5, 0.92) = 0.525
        signals = compute_exit_signals(
            open_trades    = [open_trade],
            current_prices = {open_trade.market_id: 0.55},
            hours_to_close = {open_trade.market_id: 24.0},
        )
        assert len(signals) == 1
        assert signals[0].reason == ExitReason.PROFIT_TARGET
        assert signals[0].exit_price == pytest.approx(0.55, abs=0.001)

    def test_stop_loss_hit(self, open_trade):
        # Entry at 0.35, 40% stop → exit if <= 0.21
        signals = compute_exit_signals(
            open_trades    = [open_trade],
            current_prices = {open_trade.market_id: 0.20},
            hours_to_close = {open_trade.market_id: 24.0},
        )
        assert len(signals) == 1
        assert signals[0].reason == ExitReason.EDGE_COLLAPSED

    def test_stop_loss_not_hit_at_small_drop(self, open_trade):
        # Entry 0.35, drop to 0.29 = 17% — below 40% stop, should HOLD
        signals = compute_exit_signals(
            open_trades    = [open_trade],
            current_prices = {open_trade.market_id: 0.29},
            hours_to_close = {open_trade.market_id: 24.0},
        )
        assert len(signals) == 0

    def test_time_stop(self, open_trade):
        # 15 min left — below 30-min threshold
        signals = compute_exit_signals(
            open_trades    = [open_trade],
            current_prices = {open_trade.market_id: 0.35},
            hours_to_close = {open_trade.market_id: 0.25},
        )
        assert len(signals) == 1
        assert signals[0].reason == ExitReason.TIME_STOP

    def test_market_resolved_yes_win(self, open_trade):
        # YES trade, market near 1.0 → win
        signals = compute_exit_signals(
            open_trades    = [open_trade],
            current_prices = {open_trade.market_id: 0.99},
            hours_to_close = {open_trade.market_id: 24.0},
        )
        assert len(signals) == 1
        assert signals[0].reason == ExitReason.MARKET_CLOSED
        assert signals[0].exit_price == pytest.approx(1.0)

    def test_market_resolved_yes_loss(self, open_trade):
        # YES trade, market near 0.0 → loss
        signals = compute_exit_signals(
            open_trades    = [open_trade],
            current_prices = {open_trade.market_id: 0.01},
            hours_to_close = {open_trade.market_id: 24.0},
        )
        assert len(signals) == 1
        assert signals[0].reason == ExitReason.MARKET_CLOSED
        assert signals[0].exit_price == pytest.approx(0.0)

    def test_hold_no_signal(self, open_trade):
        # Small upward move — no trigger conditions met
        signals = compute_exit_signals(
            open_trades    = [open_trade],
            current_prices = {open_trade.market_id: 0.40},
            hours_to_close = {open_trade.market_id: 24.0},
        )
        assert len(signals) == 0

    def test_missing_market_flags_close(self, open_trade):
        # Market not in scan — flagged for close at entry price
        signals = compute_exit_signals(
            open_trades    = [open_trade],
            current_prices = {},
            hours_to_close = {},
        )
        assert len(signals) == 1
        assert signals[0].reason == ExitReason.MARKET_CLOSED
        assert signals[0].exit_price == pytest.approx(open_trade.entry_price)

    def test_empty_trades_returns_empty(self):
        signals = compute_exit_signals(
            open_trades    = [],
            current_prices = {},
            hours_to_close = {},
        )
        assert signals == []
