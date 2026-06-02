"""
Phase 0.5 — bucket per-asset fills into per-market hourly time series.

Why this exists
---------------
Phase 0.4 leaves us with one Parquet per CTF token asset (YES and NO each).
Modelling needs a single per-market YES-probability time series at uniform
time steps. This module:

    1. Joins each market's two assets (YES + NO) into a single fill stream.
    2. Converts each fill to an implied YES probability via
       polybot_research.eml_node.data.probability.implied_yes_probability.
    3. Buckets those probabilities into uniform-time intervals (default 1h).
    4. Aggregates within each bucket — count, mean, VWAP, first, last.
    5. Optionally forward-fills empty buckets so the output is a regular
       grid (Phase 1 baselines need this; Phase 3 EML-NODE can use the raw
       irregular series via torchdiffeq).

Output schema (per market):
    bucket_start_ts : i64    Unix-second-aligned bucket start
    yes_prob_mean   : f64    Arithmetic mean of YES probs in bucket
    yes_prob_vwap   : f64    Volume-weighted (USDC) mean
    yes_prob_first  : f64    First fill's YES prob in bucket (by block + tx hash)
    yes_prob_last   : f64    Last fill's YES prob
    n_fills         : i64    Trades in this bucket
    n_yes_fills     : i64    From YES asset
    n_no_fills      : i64    From NO asset
    usdc_volume     : f64    Sum of USDC traded across all fills (in USDC, not micro-USDC)
    is_synthetic    : bool   True if forward-filled, False if real

USDC accounting: for ``side == "sell"``, the taker provided USDC, so
``usdc = taker_amount_filled``. For ``side == "buy"``, the maker provided
USDC, so ``usdc = maker_amount_filled``. Both are in 10⁻⁶-USDC units; we
divide by 1_000_000 to get USDC.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from polybot_research.eml_node.data.models import Fill, ResolvedMarket
from polybot_research.eml_node.data.probability import implied_yes_probability

if TYPE_CHECKING:
    import polars as pl

DEFAULT_BUCKET_SECONDS = 3600
USDC_DECIMALS = 1_000_000  # 10^-6 USDC per smallest unit

PROCESSED_DIR = Path("data/research/eml_node/processed")


# ── Pure helpers (no I/O) ─────────────────────────────────────────────────────


def _usdc_amount(fill: Fill) -> float:
    """Return the USDC volume of a single fill, in USDC (not micro-USDC)."""
    side = fill.side.lower()
    if side == "sell":
        # Taker provided USDC, maker provided tokens
        return fill.taker_amount_filled / USDC_DECIMALS
    if side == "buy":
        # Maker provided USDC, taker provided tokens
        return fill.maker_amount_filled / USDC_DECIMALS
    return 0.0


def _bucket_floor(timestamp: int, bucket_seconds: int) -> int:
    return (timestamp // bucket_seconds) * bucket_seconds


@dataclass
class _EnrichedFill:
    """Fill enriched with derived fields for bucketing — internal helper."""
    timestamp: int
    block_number: int
    transaction_hash: str
    yes_prob: float
    usdc: float
    is_yes_token: bool
    bucket_start: int


def _enrich(
    fills: list[Fill],
    *,
    is_yes_token: bool,
    bucket_seconds: int,
) -> list[_EnrichedFill]:
    """Convert raw fills to bucketing-ready records, dropping bad rows."""
    out: list[_EnrichedFill] = []
    for f in fills:
        yp = implied_yes_probability(f, is_yes_token=is_yes_token)
        if yp != yp or yp < 0.0 or yp > 1.0:
            # NaN or out-of-range probability — skip silently. These are rare
            # and indicate a malformed fill (e.g. zero amounts on either side).
            continue
        out.append(
            _EnrichedFill(
                timestamp=f.timestamp,
                block_number=f.block_number,
                transaction_hash=f.transaction_hash,
                yes_prob=yp,
                usdc=_usdc_amount(f),
                is_yes_token=is_yes_token,
                bucket_start=_bucket_floor(f.timestamp, bucket_seconds),
            )
        )
    return out


# ── Bucketing ────────────────────────────────────────────────────────────────


def bucket_market_fills(
    *,
    market: ResolvedMarket,
    yes_fills: list[Fill],
    no_fills: list[Fill],
    bucket_seconds: int = DEFAULT_BUCKET_SECONDS,
    forward_fill: bool = True,
) -> "pl.DataFrame":
    """
    Bucket the joined YES + NO fills of a single market into a YES-probability
    time series at uniform intervals.

    Parameters
    ----------
    market : ResolvedMarket
        Source of asset identity + closed_time (used as the right edge for
        forward-fill).
    yes_fills, no_fills : raw subgraph fills for the YES and NO tokens.
    bucket_seconds : 3600 = hourly. 60 = per-minute. 86400 = daily.
    forward_fill : if True, emit a regular grid from first-bucket to
        last-bucket (or market closed_time, whichever is later) and forward-
        fill the YES probability into empty buckets. If False, emit only
        buckets that contained at least one fill.

    Returns a polars DataFrame with the schema documented at module level.
    """
    import polars as pl

    enriched: list[_EnrichedFill] = []
    enriched.extend(_enrich(yes_fills, is_yes_token=True, bucket_seconds=bucket_seconds))
    enriched.extend(_enrich(no_fills, is_yes_token=False, bucket_seconds=bucket_seconds))

    if not enriched:
        logger.warning(
            "Market {} has no usable fills after enrichment; returning empty frame",
            market.id,
        )
        return _empty_frame()

    # Sort once by (block, tx_hash) so first/last within a bucket are well-defined.
    enriched.sort(key=lambda e: (e.block_number, e.transaction_hash))

    # Build a Polars DataFrame and aggregate.
    df = pl.DataFrame(
        {
            "bucket_start_ts": [e.bucket_start for e in enriched],
            "yes_prob": [e.yes_prob for e in enriched],
            "usdc": [e.usdc for e in enriched],
            "is_yes_token": [e.is_yes_token for e in enriched],
            "block_number": [e.block_number for e in enriched],
        }
    )

    # USDC-VWAP: weighted sum / total weight. Guard div-by-zero with coalesce.
    agg = df.group_by("bucket_start_ts").agg(
        [
            pl.col("yes_prob").mean().alias("yes_prob_mean"),
            (
                (pl.col("yes_prob") * pl.col("usdc")).sum()
                / pl.col("usdc").sum().clip(lower_bound=1e-12)
            ).alias("yes_prob_vwap"),
            pl.col("yes_prob").first().alias("yes_prob_first"),
            pl.col("yes_prob").last().alias("yes_prob_last"),
            pl.len().alias("n_fills"),
            pl.col("is_yes_token").sum().cast(pl.Int64).alias("n_yes_fills"),
            (~pl.col("is_yes_token")).sum().cast(pl.Int64).alias("n_no_fills"),
            pl.col("usdc").sum().alias("usdc_volume"),
        ]
    ).sort("bucket_start_ts").with_columns(
        pl.lit(False).alias("is_synthetic")
    )

    if not forward_fill:
        return agg

    return _forward_fill_grid(agg, market=market, bucket_seconds=bucket_seconds)


def _empty_frame() -> "pl.DataFrame":
    import polars as pl

    return pl.DataFrame(
        schema={
            "bucket_start_ts": pl.Int64,
            "yes_prob_mean": pl.Float64,
            "yes_prob_vwap": pl.Float64,
            "yes_prob_first": pl.Float64,
            "yes_prob_last": pl.Float64,
            "n_fills": pl.Int64,
            "n_yes_fills": pl.Int64,
            "n_no_fills": pl.Int64,
            "usdc_volume": pl.Float64,
            "is_synthetic": pl.Boolean,
        }
    )


def _forward_fill_grid(
    agg: "pl.DataFrame",
    *,
    market: ResolvedMarket,
    bucket_seconds: int,
) -> "pl.DataFrame":
    """
    Expand `agg` to a uniform grid of bucket_start_ts and forward-fill the
    YES probability into empty buckets. Synthetic rows have n_fills=0,
    usdc_volume=0, is_synthetic=True.

    The grid runs from the first real bucket through max(last real bucket,
    market.closed_time bucket) so the series always covers the full life of
    the market right up to resolution.
    """
    import polars as pl

    if agg.is_empty():
        return agg

    first_bucket = int(agg["bucket_start_ts"].min())
    last_real_bucket = int(agg["bucket_start_ts"].max())

    # If we know when the market closed, extend the grid to cover it.
    last_bucket = last_real_bucket
    if market.closed_time:
        try:
            from datetime import datetime, timezone

            # Gamma's closed_time format is "2026-02-14 00:09:43+00"
            closed_str = market.closed_time.replace(" ", "T")
            if closed_str.endswith("+00"):
                closed_str = closed_str[:-3] + "+00:00"
            closed_dt = datetime.fromisoformat(closed_str)
            if closed_dt.tzinfo is None:
                closed_dt = closed_dt.replace(tzinfo=timezone.utc)
            closed_ts = int(closed_dt.timestamp())
            last_bucket = max(last_bucket, _bucket_floor(closed_ts, bucket_seconds))
        except (ValueError, TypeError) as exc:
            logger.debug(
                "Could not parse closed_time {!r} for market {}: {}",
                market.closed_time,
                market.id,
                exc,
            )

    full_grid = pl.DataFrame(
        {
            "bucket_start_ts": list(
                range(first_bucket, last_bucket + bucket_seconds, bucket_seconds)
            )
        }
    )

    joined = full_grid.join(agg, on="bucket_start_ts", how="left").sort(
        "bucket_start_ts"
    )

    # Mark synthetic rows BEFORE forward-fill (they have null yes_prob_*).
    joined = joined.with_columns(
        pl.col("yes_prob_mean").is_null().alias("is_synthetic"),
        pl.col("n_fills").fill_null(0),
        pl.col("n_yes_fills").fill_null(0),
        pl.col("n_no_fills").fill_null(0),
        pl.col("usdc_volume").fill_null(0.0),
    )

    # Forward-fill the four price columns.
    joined = joined.with_columns(
        [
            pl.col("yes_prob_mean").forward_fill(),
            pl.col("yes_prob_vwap").forward_fill(),
            pl.col("yes_prob_first").forward_fill(),
            pl.col("yes_prob_last").forward_fill(),
        ]
    )

    return joined


# ── Persistence ──────────────────────────────────────────────────────────────


def _bucket_path(market: ResolvedMarket) -> Path:
    """One Parquet per market, keyed by condition_id (full hex hash, unique)."""
    return PROCESSED_DIR / f"{market.condition_id}.parquet"


def write_market_buckets(market: ResolvedMarket, buckets: "pl.DataFrame") -> Path:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out = _bucket_path(market)
    buckets.write_parquet(out)
    return out


def bucket_all_kept_markets(
    kept: list[ResolvedMarket],
    fills_map: dict[str, list[Fill]],
    *,
    bucket_seconds: int = DEFAULT_BUCKET_SECONDS,
    forward_fill: bool = True,
) -> dict[str, Path]:
    """
    Stage 0.5 driver. For each kept market, look up its YES + NO fills in
    fills_map (keyed by asset_id), bucket them, write per-market Parquet.

    Returns {condition_id: parquet_path}.
    """
    written: dict[str, Path] = {}
    for m in kept:
        ids = m.clob_token_ids
        if len(ids) != 2:
            logger.warning(
                "Skipping market {} with {} token IDs (expected 2)",
                m.id,
                len(ids),
            )
            continue
        yes_id, no_id = ids[0], ids[1]
        yes_fills = fills_map.get(yes_id, [])
        no_fills = fills_map.get(no_id, [])
        if not yes_fills and not no_fills:
            logger.warning("Market {} has no fills in fills_map; skipping", m.id)
            continue

        buckets = bucket_market_fills(
            market=m,
            yes_fills=yes_fills,
            no_fills=no_fills,
            bucket_seconds=bucket_seconds,
            forward_fill=forward_fill,
        )
        if buckets.is_empty():
            logger.warning("Market {} produced empty bucket frame", m.id)
            continue

        path = write_market_buckets(m, buckets)
        written[m.condition_id] = path
        logger.info(
            "Bucketed market {} → {} rows ({} synthetic) → {}",
            m.id,
            buckets.height,
            int(buckets["is_synthetic"].sum()),
            path.name,
        )
    logger.info("Phase 0.5 wrote {} per-market bucket files", len(written))
    return written
