"""
Exit strategy engine.

Responsible for deciding WHEN to close open paper positions.
Two triggers:

1. Profit target: exit when market price moves enough in our favour
   - We entered at 0.245 (YES), price is now 0.72 → take profit
   - Default target: 2x the entry price, or the edge collapses

2. Resolution / market closed: exit at final price (0.0 or 1.0)

The analogy: a stop-loss is the parking brake. A profit target is
knowing which floor you're getting off on. Without both, you either
hold forever or panic-sell at the wrong moment.
"""

from __future__ import annotations

from dataclasses import dataclass
import sys
if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from enum import Enum
    class StrEnum(str, Enum):
        def __str__(self) -> str:
            return self.value

from loguru import logger

from polybot.models import PaperTrade, Side


class ExitReason(StrEnum):
    PROFIT_TARGET  = "profit_target"
    EDGE_COLLAPSED = "edge_collapsed"
    MARKET_CLOSED  = "market_closed"
    TIME_STOP      = "time_stop"        # market closing in < 30 min


@dataclass
class ExitSignal:
    trade_id:       str
    reason:         ExitReason
    exit_price:     float
    current_price:  float
    note:           str


def compute_exit_signals(
    open_trades:    list[PaperTrade],
    current_prices: dict[str, float],    # market_id → current YES price
    hours_to_close: dict[str, float],    # market_id → hours remaining
    *,
    profit_target_multiplier: float = 1.8,   # exit when price = entry * 1.8
    edge_collapse_threshold:  float = 0.03,  # edge fell below 3%
    time_stop_hours:          float = 0.5,   # close if < 30 min to resolution
) -> list[ExitSignal]:
    """
    For each open position, decide whether to exit and at what price.
    Returns a list of ExitSignals — caller executes the actual closes.
    """
    signals: list[ExitSignal] = []

    for trade in open_trades:
        current_yes = current_prices.get(trade.market_id)
        if current_yes is None:
            # Market not in latest scan — may have closed or been delisted
            logger.warning(f"No current price for market {trade.market_id} — flagging for close")
            signals.append(ExitSignal(
                trade_id      = trade.id,
                reason        = ExitReason.MARKET_CLOSED,
                exit_price    = trade.entry_price,   # conservative: no gain/loss
                current_price = trade.entry_price,
                note          = "Market disappeared from scanner",
            ))
            continue

        # Convert YES price to our-side price
        current_side_price = current_yes if trade.side == Side.YES else (1 - current_yes)
        hours_left = hours_to_close.get(trade.market_id, 999.0)

        # ── Time stop ──────────────────────────────────────────────────────────
        if hours_left < time_stop_hours:
            signals.append(ExitSignal(
                trade_id      = trade.id,
                reason        = ExitReason.TIME_STOP,
                exit_price    = current_side_price,
                current_price = current_side_price,
                note          = f"Market closing in {hours_left:.1f}h",
            ))
            logger.info(
                f"⏰ TIME STOP {trade.question[:45]} | "
                f"exit={current_side_price:.3f} | {hours_left:.1f}h left"
            )
            continue

        # ── Market resolved (price at 0 or 1) ─────────────────────────────────
        if current_yes >= 0.98 or current_yes <= 0.02:
            final_price = 1.0 if (
                (trade.side == Side.YES and current_yes >= 0.98) or
                (trade.side == Side.NO  and current_yes <= 0.02)
            ) else 0.0
            signals.append(ExitSignal(
                trade_id      = trade.id,
                reason        = ExitReason.MARKET_CLOSED,
                exit_price    = final_price,
                current_price = current_side_price,
                note          = f"Market resolved YES={current_yes:.3f}",
            ))
            logger.info(
                f"🏁 RESOLVED {trade.question[:45]} | "
                f"final={final_price} side={trade.side}"
            )
            continue

        # ── Profit target ──────────────────────────────────────────────────────
        target = min(trade.entry_price * profit_target_multiplier, 0.92)
        if current_side_price >= target:
            signals.append(ExitSignal(
                trade_id      = trade.id,
                reason        = ExitReason.PROFIT_TARGET,
                exit_price    = current_side_price,
                current_price = current_side_price,
                note          = f"Target hit: entry={trade.entry_price:.3f} → {current_side_price:.3f}",
            ))
            logger.info(
                f"✅ PROFIT TARGET {trade.question[:40]} | "
                f"entry={trade.entry_price:.3f} exit={current_side_price:.3f}"
            )
            continue

        # ── Edge collapse: price moved against us past entry ───────────────────
        if current_side_price < (trade.entry_price - edge_collapse_threshold):
            signals.append(ExitSignal(
                trade_id      = trade.id,
                reason        = ExitReason.EDGE_COLLAPSED,
                exit_price    = current_side_price,
                current_price = current_side_price,
                note          = (
                    f"Edge collapsed: entry={trade.entry_price:.3f} "
                    f"now={current_side_price:.3f}"
                ),
            ))
            logger.info(
                f"⚠️  EDGE COLLAPSED {trade.question[:40]} | "
                f"entry={trade.entry_price:.3f} now={current_side_price:.3f}"
            )
            continue

        logger.debug(
            f"HOLD {trade.question[:40]} | "
            f"entry={trade.entry_price:.3f} now={current_side_price:.3f} "
            f"target={target:.3f} {hours_left:.1f}h left"
        )

    return signals