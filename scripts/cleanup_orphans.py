"""One-shot cleanup: close orphan positions in archived/corrupted trade logs.

An *orphan* is a position whose latest row in its source file is still
``status="open"``. This happens when the trade log was archived or corrupted
before the bot got a chance to log a close. Each orphan inflates the open
count in future audits even though no live bot is tracking it.

This script appends a synthetic ``status="abandoned"`` row per orphan,
preserving the original open row. The new status is intentionally distinct
from ``closed`` so it doesn't pollute the closed-trade P&L analysis.

Targets only ARCHIVED / CORRUPTED files. The active ``trades.jsonl`` is
owned by the running engine and is left alone (the engine dedupes orphans
correctly on load via ``_load_history``).

Idempotent: re-runs find zero orphans because the appended ``abandoned`` row
becomes the new latest record per opportunity_id.

Usage:
    uv run --no-sync python scripts/cleanup_orphans.py            # dry run
    uv run --no-sync python scripts/cleanup_orphans.py --commit   # write
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TRADES_DIR = ROOT / "data" / "trades"

TARGET_FILES = [
    "paper_trades.ARCHIVED_20260404.jsonl",
    "trades.CORRUPTED_20260516.jsonl",
]

TERMINAL_STATUSES = {"closed", "abandoned"}


def find_orphans(path: Path) -> list[dict]:
    """Return one row per opp_id whose latest record is still ``open``."""
    latest: dict[str, dict] = {}
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            opp = rec.get("opportunity_id")
            if opp is None:
                continue
            latest[opp] = rec
    return [r for r in latest.values() if r.get("status") == "open"]


def make_abandoned(rec: dict, closed_at: str, run_at: str) -> dict:
    out = dict(rec)
    out["status"]         = "abandoned"
    out["closed_at"]      = closed_at
    out["exit_price"]     = None
    out["pnl_usd"]        = 0.0
    out["pnl_pct"]        = 0.0
    out["cleanup_run"]    = run_at
    out["cleanup_reason"] = "orphan_no_close_recorded"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="Write changes (default: dry run)")
    args = ap.parse_args()

    run_at = datetime.now(timezone.utc).isoformat()
    grand_total = 0

    for name in TARGET_FILES:
        path = TRADES_DIR / name
        if not path.exists():
            print(f"[skip] {name} — not found")
            continue

        orphans = find_orphans(path)
        print(f"[{name}] {len(orphans)} orphan(s)")
        grand_total += len(orphans)
        if not orphans:
            continue

        # File mtime is the closest proxy we have for when the bot last
        # touched these positions.
        closed_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()

        if not args.commit:
            for rec in orphans[:3]:
                print(f"  would close: {rec['opportunity_id']}  {rec.get('question','')[:60]}")
            if len(orphans) > 3:
                print(f"  ... and {len(orphans) - 3} more")
            continue

        backup = path.with_suffix(path.suffix + ".bak-before-orphan-cleanup")
        if not backup.exists():
            shutil.copy2(path, backup)
            print(f"  backup → {backup.name}")
        else:
            print(f"  backup already exists at {backup.name}, leaving as-is")

        with path.open("a") as f:
            for rec in orphans:
                f.write(json.dumps(make_abandoned(rec, closed_at, run_at)) + "\n")
        print(f"  wrote {len(orphans)} abandoned record(s)")

    mode = "COMMITTED" if args.commit else "DRY RUN"
    print(f"\n[{mode}] {grand_total} orphan(s) across {len(TARGET_FILES)} file(s)")
    if not args.commit:
        print("Re-run with --commit to write changes.")


if __name__ == "__main__":
    main()
