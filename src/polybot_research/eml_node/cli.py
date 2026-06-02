"""
CLI entry points for the EML × Polymarket research project.

Wired into pyproject.toml as console_scripts:

    eml-schema-check   →  Phase 0.0 — verify subgraph schema, snapshot to disk
    eml-phase0         →  Phase 0.2–0.4 — discover, filter, pull fills
    eml-status         →  Quick summary of what's on disk

Run these in the project root via ``uv run``:

    uv run eml-schema-check
    uv run eml-phase0
    uv run eml-status
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from polybot_research.eml_node.data.pipeline import run_phase_0
from polybot_research.eml_node.data.sanity import run_sanity
from polybot_research.eml_node.data.schema_check import _main as _schema_check_main

# Load .env on CLI entry so GRAPH_API_KEY (and any future research env vars)
# are visible without requiring the user to source them manually. The live
# polybot CLI does this implicitly via polybot.config.Settings; our research
# CLI doesn't depend on that, so we load directly.
load_dotenv()


def schema_check() -> None:
    """Run subgraph schema introspection + snapshot diff."""
    raise SystemExit(asyncio.run(_schema_check_main()))


def phase0() -> None:
    """Run Phase 0 stages 0.2–0.6 with conservative defaults."""
    asyncio.run(run_phase_0())


def sanity() -> None:
    """Run Phase 0.6 sanity cross-check against existing on-disk data."""
    metrics = run_sanity()
    if metrics["n_failed"] > 0:
        raise SystemExit(1)


def status() -> None:
    """Report what artifacts exist under data/research/eml_node/."""
    root = Path("data/research/eml_node")
    if not root.exists():
        logger.warning("No data/research/eml_node directory yet. Run eml-phase0 first.")
        return

    snapshots = sorted((root / "schema_snapshots").glob("*.json"))
    raw = root / "interim" / "gamma_markets_raw_v1.json"
    index = root / "interim" / "markets_index.parquet"
    fills_dir = root / "interim" / "fills"
    fills = sorted(fills_dir.glob("*.parquet")) if fills_dir.exists() else []
    processed_dir = root / "processed"
    # Bucketed market files are *.parquet excluding the leading-underscore
    # report files (e.g. _sanity_report.parquet).
    processed = (
        sorted(p for p in processed_dir.glob("*.parquet") if not p.name.startswith("_"))
        if processed_dir.exists()
        else []
    )
    sanity_report = processed_dir / "_sanity_report.parquet"

    logger.info("=== eml-node project status ===")
    logger.info(
        "Schema snapshots : {} (most recent: {})",
        len(snapshots),
        snapshots[-1].name if snapshots else "none",
    )
    logger.info(
        "Raw Gamma dump   : {}",
        f"{raw} ({raw.stat().st_size:,} bytes)" if raw.exists() else "missing",
    )
    logger.info(
        "Markets index    : {}",
        f"{index} ({index.stat().st_size:,} bytes)" if index.exists() else "missing",
    )
    logger.info(
        "Fills parquets   : {} files in {}",
        len(fills),
        fills_dir,
    )
    logger.info(
        "Bucketed series  : {} files in {}",
        len(processed),
        processed_dir,
    )
    if sanity_report.exists():
        import polars as pl

        df = pl.read_parquet(sanity_report)
        n_pass = int(df["passed"].sum())
        n_total = df.height
        logger.info(
            "Sanity report    : {} markets ({} passed, {} failed)",
            n_total,
            n_pass,
            n_total - n_pass,
        )
    else:
        logger.info("Sanity report    : missing (run `eml-sanity`)")
