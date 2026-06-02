"""
Phase 0.0 — schema introspection codified.

Runs the GraphQL introspection queries against the subgraph and saves the
responses as JSON snapshots under data/research/eml_node/schema_snapshots/.
On subsequent runs, diffs the new snapshot against the most recent one and
fails (or warns, depending on flag) if the schema has drifted.

This is the early-warning system that catches "the subgraph upstream silently
changed a field name" before it produces nonsense data downstream.

Usage
-----
    uv run python -m polybot_research.eml_node.data.schema_check
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

from loguru import logger

from polybot_research.eml_node.data.subgraph import SubgraphClient

SNAPSHOT_DIR = Path("data/research/eml_node/schema_snapshots")
TYPES_OF_INTEREST = ("OrderFilledEvent", "MarketData", "Orderbook")


def _today_path() -> Path:
    return SNAPSHOT_DIR / f"{date.today().isoformat()}.json"


def _most_recent_snapshot(exclude: Path | None = None) -> Path | None:
    if not SNAPSHOT_DIR.exists():
        return None
    snapshots = sorted(p for p in SNAPSHOT_DIR.glob("*.json") if p != exclude)
    return snapshots[-1] if snapshots else None


async def _capture() -> dict[str, object]:
    async with SubgraphClient() as client:
        snapshot: dict[str, object] = {
            "schema_types": await client.list_types(),
        }
        for type_name in TYPES_OF_INTEREST:
            snapshot[type_name] = await client.introspect_type(type_name)
        return snapshot


def _diff(old: dict[str, object], new: dict[str, object]) -> list[str]:
    """
    Return human-readable diff messages. Empty list = no drift.

    Compares only keys we actually rely on; ignores ordering and whitespace.
    """
    diffs: list[str] = []
    old_str = json.dumps(old, sort_keys=True, indent=2)
    new_str = json.dumps(new, sort_keys=True, indent=2)
    if old_str != new_str:
        diffs.append(
            "Schema content differs from previous snapshot. "
            "Inspect both files manually to identify field-level changes."
        )
    return diffs


async def _main() -> int:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = await _capture()

    out_path = _today_path()
    out_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True))
    logger.info("Wrote schema snapshot: {}", out_path)

    previous = _most_recent_snapshot(exclude=out_path)
    if previous is None:
        logger.info("No prior snapshot to diff against. This is the baseline.")
        return 0

    previous_data = json.loads(previous.read_text())
    diffs = _diff(previous_data, snapshot)
    if diffs:
        logger.warning("Schema drift detected vs {}:", previous.name)
        for d in diffs:
            logger.warning("  - {}", d)
        return 1

    logger.info("No schema drift vs {}", previous.name)
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
