"""Audit historical trade log: paper vs live mix, P&L, win rate, edge calibration.

Reads data/trades/*.jsonl and reports whether the bot has demonstrated edge.

Per (source_file, opportunity_id) is the natural unit — each roundtrip writes
two rows (open + close) and counting them separately inflates the open count.
The engine dedupes the same way in `_load_history`.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import polars as pl


ROOT = Path(__file__).resolve().parent.parent
TRADES_DIR = ROOT / "data" / "trades"
LIVE_LOG = "trades.jsonl"


def load_trades() -> pl.DataFrame:
    rows: list[dict] = []
    for path in sorted(TRADES_DIR.glob("*.jsonl")):
        if path.name.startswith("._"):
            continue
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rec["_source_file"] = path.name
                rows.append(rec)
    if not rows:
        print("No trades found.")
        sys.exit(0)
    return pl.DataFrame(rows, infer_schema_length=None)


def dedupe_latest(df: pl.DataFrame) -> pl.DataFrame:
    """Collapse to one row per (source_file, opportunity_id), keeping the latest.

    Each position appears twice per roundtrip (open + close) — for status-mix
    accounting we want the terminal state, not the history. Same dedupe the
    engine does in `_load_history`.
    """
    return (
        df.with_row_index("_row")
        .sort("_row")
        .unique(subset=["_source_file", "opportunity_id"], keep="last", maintain_order=True)
        .drop("_row")
    )


def classify_mode(df: pl.DataFrame) -> pl.DataFrame:
    cols = df.columns
    has_clob = "clob_order_id" in cols
    has_us   = "live_order_id" in cols
    if has_clob and has_us:
        is_live = (pl.col("clob_order_id").is_not_null()) | (pl.col("live_order_id").is_not_null())
    elif has_clob:
        is_live = pl.col("clob_order_id").is_not_null()
    elif has_us:
        is_live = pl.col("live_order_id").is_not_null()
    else:
        is_live = pl.lit(False)
    return df.with_columns(mode=pl.when(is_live).then(pl.lit("live")).otherwise(pl.lit("paper")))


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def report_overall(raw: pl.DataFrame, terminal: pl.DataFrame) -> None:
    section("Overall")
    print(f"Raw log rows:   {raw.height}")
    print(f"Unique opps:    {terminal.height}")
    print(f"Status mix:     {dict(terminal.group_by('status').len().iter_rows())}")
    print(f"Mode mix:       {dict(terminal.group_by('mode').len().iter_rows())}")
    by_file = (
        terminal.group_by(["_source_file", "status"])
        .len()
        .sort(["_source_file", "status"])
    )
    print("Per-file status:")
    for src, st, n in by_file.iter_rows():
        print(f"  {src:48s} {st:10s} {n}")
    if "opened_at" in terminal.columns:
        opened = terminal["opened_at"].drop_nulls()
        if opened.len():
            print(f"Date range:     {opened.min()}  →  {opened.max()}")


def report_duplicate_closes(raw: pl.DataFrame) -> None:
    """Surface concurrent-write corruption: opps that were closed >1 time.

    Each opp_id is a one-shot — `Opportunity.id` is a fresh token_hex per scan
    and `close_position` pops from self.positions. Multiple closed rows for
    the same opp_id means multiple bot processes wrote concurrently before the
    instance lock landed (cli.py). Their summed P&L is double-counted and the
    pre-lock archive numbers are not trustworthy.
    """
    section("Duplicate-close audit (concurrent-write corruption)")
    closed = raw.filter(pl.col("status") == "closed")
    counts = (
        closed.group_by(["_source_file", "opportunity_id"])
        .len()
        .filter(pl.col("len") > 1)
    )
    if counts.is_empty():
        print("No duplicate closes detected.")
        return
    by_file = (
        counts.group_by("_source_file")
        .agg(
            dup_opps=pl.len(),
            extra_rows=(pl.col("len") - 1).sum(),
            worst=pl.col("len").max(),
        )
        .sort("_source_file")
    )
    print(by_file)
    print(
        f"Total extra rows: {int((counts['len'] - 1).sum())}  "
        f"(closed row count is inflated by this many)"
    )


def report_live_open(df: pl.DataFrame) -> None:
    """Open positions in the active trade log — what the engine loads on restart."""
    section("Live open positions (engine view)")
    live = df.filter(
        (pl.col("_source_file") == LIVE_LOG) & (pl.col("status") == "open")
    )
    print(f"Count: {live.height}")
    if live.is_empty():
        return
    cols = [
        c for c in ["opportunity_id", "side", "entry_price", "size_usd", "opened_at", "question"]
        if c in live.columns
    ]
    with pl.Config(tbl_rows=-1, fmt_str_lengths=80, tbl_width_chars=200):
        print(live.select(cols).sort("opened_at"))


def report_closed(df: pl.DataFrame) -> None:
    closed = df.filter(pl.col("status") == "closed")
    section(f"Closed trades — {closed.height}")
    if closed.is_empty():
        print("(none)")
        return
    closed = closed.with_columns(
        pnl_usd=pl.col("pnl_usd").cast(pl.Float64, strict=False).fill_null(0.0),
        win=pl.col("pnl_usd").cast(pl.Float64, strict=False).fill_null(0.0) > 0,
    )
    total = closed["pnl_usd"].sum()
    n = closed.height
    wins = closed["win"].sum()
    avg = closed["pnl_usd"].mean()
    median = closed["pnl_usd"].median()
    best = closed["pnl_usd"].max()
    worst = closed["pnl_usd"].min()
    print(f"Total P&L:      ${total:+,.2f}")
    print(f"Win rate:       {wins}/{n}  = {wins / n:.1%}")
    print(f"Avg P&L/trade:  ${avg:+.4f}")
    print(f"Median:         ${median:+.4f}")
    print(f"Best / Worst:   ${best:+.2f} / ${worst:+.2f}")

    # by side
    section("By side")
    by_side = (
        closed.group_by("side")
        .agg(
            n=pl.len(),
            wins=pl.col("win").sum(),
            pnl=pl.col("pnl_usd").sum(),
            avg=pl.col("pnl_usd").mean(),
        )
        .with_columns(win_rate=pl.col("wins") / pl.col("n"))
        .sort("side")
    )
    print(by_side)

    # by mode
    section("By mode (paper vs live)")
    by_mode = (
        closed.group_by("mode")
        .agg(
            n=pl.len(),
            wins=pl.col("win").sum(),
            pnl=pl.col("pnl_usd").sum(),
            avg=pl.col("pnl_usd").mean(),
        )
        .with_columns(win_rate=pl.col("wins") / pl.col("n"))
        .sort("mode")
    )
    print(by_mode)

    # by entry-price bucket (extremes have higher loss potential)
    section("By entry price bucket")
    buckets = closed.with_columns(
        bucket=pl.col("entry_price").cast(pl.Float64).cut(
            breaks=[0.1, 0.25, 0.5, 0.75, 0.9],
            labels=["<0.10", "0.10-0.25", "0.25-0.50", "0.50-0.75", "0.75-0.90", ">0.90"],
        )
    )
    by_bucket = (
        buckets.group_by("bucket")
        .agg(
            n=pl.len(),
            wins=pl.col("win").sum(),
            pnl=pl.col("pnl_usd").sum(),
            avg=pl.col("pnl_usd").mean(),
        )
        .with_columns(win_rate=pl.col("wins") / pl.col("n"))
        .sort("bucket")
    )
    print(by_bucket)

    # cumulative P&L over time
    section("Daily P&L (closed trades)")
    if "closed_at" in closed.columns:
        daily = (
            closed.with_columns(day=pl.col("closed_at").str.slice(0, 10))
            .group_by("day")
            .agg(n=pl.len(), pnl=pl.col("pnl_usd").sum())
            .sort("day")
            .with_columns(cum_pnl=pl.col("pnl").cum_sum())
        )
        with pl.Config(tbl_rows=-1):
            print(daily)


def main() -> None:
    raw = load_trades()
    raw = classify_mode(raw)
    terminal = dedupe_latest(raw)
    report_overall(raw, terminal)
    report_duplicate_closes(raw)
    report_live_open(terminal)
    report_closed(terminal)


if __name__ == "__main__":
    main()
