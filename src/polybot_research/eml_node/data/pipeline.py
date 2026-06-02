"""
Phase 0 orchestration — Gamma discovery → filter → subgraph fills → Parquet snapshot.

Pipeline stages
---------------
0.0  Schema introspection (separate script: schema_check.py)
0.1  Gamma `/markets` access — verified at scaffold time
0.2  Pull resolved-market universe → keep only markets passing the audit filter
0.3  Save markets_index.parquet keyed by condition_id
0.4  For each kept market, pull all OrderFilledEvents from subgraph
0.5  Bucket fills into hourly time series
0.6  Sanity cross-check (final pre-resolution price vs Gamma outcome_prices)
0.7  Train / dev / held-out test split (20% holdout never touched until Phase 5)

This module currently implements stages 0.2 → 0.4 end-to-end. Bucketing,
cross-check, and split (0.5–0.7) are TODO; they wire on the same data once
fills are persisted.

Output paths (all under data/research/eml_node/):
    interim/gamma_markets_raw_v1.json     — every market Gamma returned (pre-filter)
    interim/markets_index.parquet         — kept markets, parsed and indexed
    interim/fills/<asset_id>.parquet      — fills per CTF token asset
    processed/<condition_id>.parquet      — bucketed YES-probability time series per market
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger

from polybot_research.eml_node.data.bucketing import (
    DEFAULT_BUCKET_SECONDS,
    bucket_all_kept_markets,
)
from polybot_research.eml_node.data.filter import keep_resolved_market
from polybot_research.eml_node.data.gamma import ResolvedMarketsClient
from polybot_research.eml_node.data.models import Fill, ResolvedMarket
from polybot_research.eml_node.data.sanity import (
    DEFAULT_THRESHOLD,
    check_all_markets,
    summarize,
    write_sanity_report,
)
from polybot_research.eml_node.data.subgraph import SubgraphClient

DATA_ROOT = Path("data/research/eml_node")
INTERIM = DATA_ROOT / "interim"
FILLS_DIR = INTERIM / "fills"


def _to_fills_path(asset_id: str) -> Path:
    """Asset IDs are giant uint256 strings; truncate for filesystem sanity."""
    return FILLS_DIR / f"{asset_id[:16]}__{asset_id[-8:]}.parquet"


async def discover_markets(
    *,
    max_markets: int = 500,
    min_clob_volume_usd: float = 1_000.0,
    save_raw: bool = True,
) -> tuple[list[ResolvedMarket], list[ResolvedMarket]]:
    """
    Stage 0.2 + 0.3: pull resolved markets from Gamma, apply audit filter.

    Returns (kept, dropped) so the caller can inspect filter behavior.
    """
    INTERIM.mkdir(parents=True, exist_ok=True)

    raw_dump: list[dict[str, Any]] = []
    kept: list[ResolvedMarket] = []
    dropped: list[ResolvedMarket] = []

    async with ResolvedMarketsClient() as client:
        async for market in client.iter_resolved(max_markets=max_markets):
            if save_raw:
                raw_dump.append(market.model_dump(by_alias=True))
            if keep_resolved_market(market, min_clob_volume_usd=min_clob_volume_usd):
                kept.append(market)
            else:
                dropped.append(market)

    if save_raw:
        out = INTERIM / "gamma_markets_raw_v1.json"
        out.write_text(json.dumps(raw_dump, default=str, indent=2))
        logger.info("Wrote raw Gamma dump: {} ({} markets)", out, len(raw_dump))

    logger.info(
        "Filter result: {} kept / {} dropped (of {} fetched)",
        len(kept),
        len(dropped),
        len(raw_dump),
    )
    return kept, dropped


def write_markets_index(kept: list[ResolvedMarket]) -> Path:
    """
    Stage 0.3 (write): persist the kept markets as Parquet keyed by condition_id.

    pyarrow is in the [research] optional deps. If you see ImportError here,
    install with: uv sync --extra research
    """
    try:
        import polars as pl
    except ImportError as exc:
        raise RuntimeError(
            "polars is required to write Parquet. "
            "Install research extras: `uv sync --extra research`"
        ) from exc

    rows = []
    for m in kept:
        rows.append(
            {
                "id": m.id,
                "condition_id": m.condition_id,
                "question": m.question,
                "slug": m.slug,
                "clob_token_id_yes": (
                    m.clob_token_ids[0] if len(m.clob_token_ids) >= 1 else None
                ),
                "clob_token_id_no": (
                    m.clob_token_ids[1] if len(m.clob_token_ids) >= 2 else None
                ),
                "yes_won": m.yes_won,
                "closed_time": m.closed_time,
                "end_date": m.end_date.isoformat() if m.end_date else None,
                "volume": m.volume,
                "volume_1yr_clob": m.volume_1yr_clob,
                "volume_1mo_clob": m.volume_1mo_clob,
                "liquidity_clob": m.liquidity_clob,
            }
        )
    df = pl.DataFrame(rows)
    out = INTERIM / "markets_index.parquet"
    df.write_parquet(out)
    logger.info("Wrote markets index: {} ({} rows)", out, df.height)
    return out


async def pull_fills_for_kept_markets(
    kept: list[ResolvedMarket],
    *,
    max_fills_per_asset: int | None = None,
    asset_concurrency: int = 4,
) -> dict[str, list[Fill]]:
    """
    Stage 0.4: for each kept market, pull all fills for both YES and NO tokens.

    Persists per-asset Parquet to interim/fills/. Returns an in-memory map
    {asset_id: [Fill, ...]} for the caller to inspect.
    """
    FILLS_DIR.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(asset_concurrency)
    results: dict[str, list[Fill]] = {}

    async def _pull_one(asset_id: str, subgraph: SubgraphClient) -> None:
        async with sem:
            page_cap = (
                None
                if max_fills_per_asset is None
                else max(1, max_fills_per_asset // 1000)
            )
            fills = await subgraph.fetch_fills_for_asset(
                asset_id, max_pages=page_cap
            )
            results[asset_id] = fills
            logger.info("Asset {}…: {} fills", asset_id[:12], len(fills))
            _write_fills_parquet(asset_id, fills)

    async with SubgraphClient() as subgraph:
        tasks = []
        for m in kept:
            for asset_id in m.clob_token_ids:
                if not asset_id:
                    continue
                tasks.append(_pull_one(asset_id, subgraph))
        await asyncio.gather(*tasks)

    logger.info(
        "Pulled fills for {} assets across {} markets",
        len(results),
        len(kept),
    )
    return results


def _write_fills_parquet(asset_id: str, fills: list[Fill]) -> Path:
    try:
        import polars as pl
    except ImportError as exc:
        raise RuntimeError(
            "polars required. Install with: `uv sync --extra research`"
        ) from exc

    df = pl.DataFrame([f.model_dump() for f in fills])
    out = _to_fills_path(asset_id)
    df.write_parquet(out)
    return out


async def run_phase_0(
    *,
    max_markets: int = 500,
    max_fills_per_asset: int | None = 5000,
    min_clob_volume_usd: float = 1_000.0,
    bucket_seconds: int = DEFAULT_BUCKET_SECONDS,
    forward_fill_buckets: bool = True,
    sanity_threshold: float = DEFAULT_THRESHOLD,
) -> None:
    """
    Top-level Phase 0 driver — discover, filter, persist markets index, pull fills,
    bucket per-market YES-probability time series, run sanity cross-check.

    Conservative defaults so a first run is fast and audit-able. Bump
    max_markets up once filter behavior is verified empirically.
    """
    kept, _dropped = await discover_markets(
        max_markets=max_markets,
        min_clob_volume_usd=min_clob_volume_usd,
        save_raw=True,
    )
    if not kept:
        logger.warning(
            "No markets passed the filter. Review interim/gamma_markets_raw_v1.json "
            "and the predicate in polybot_research.eml_node.data.filter."
        )
        return

    write_markets_index(kept)
    fills_map = await pull_fills_for_kept_markets(
        kept, max_fills_per_asset=max_fills_per_asset
    )

    # Stage 0.5 — bucket fills into per-market YES-probability time series.
    bucket_all_kept_markets(
        kept,
        fills_map,
        bucket_seconds=bucket_seconds,
        forward_fill=forward_fill_buckets,
    )

    # Stage 0.6 — sanity cross-check: final pre-resolution YES prob vs Gamma.
    sanity_results = check_all_markets(kept, threshold=sanity_threshold)
    write_sanity_report(sanity_results)
    summarize(sanity_results)

    logger.info(
        "Phase 0 (stages 0.2–0.6) complete. Next: train/dev/test split (0.7)."
    )


def main() -> None:
    asyncio.run(run_phase_0())


if __name__ == "__main__":
    main()
