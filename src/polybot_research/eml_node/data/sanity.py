"""
Phase 0.6 — sanity cross-check on bucketed YES-probability series.

For every market in the dataset, check that the final pre-resolution YES
probability (taken from the last real, non-synthetic bucket's
``yes_prob_last``) agrees with Gamma's resolution outcome:

    if yes_won:  expected_final_yes_prob = 1.0
    else:        expected_final_yes_prob = 0.0

A discrepancy larger than the configured threshold flags a bug — most likely
in the YES/NO asset assignment (clob_token_ids order), the probability rule,
or the bucketing aggregation. Markets that converge in the wrong direction
are a *load-bearing* check; if a meaningful fraction fail, the dataset
cannot be trusted for Phase 1 baselines or Phase 3 EML-NODE training.

Why threshold default = 0.05
----------------------------
Polymarket CLOB has a minimum tick size of 0.001, so the closest tradeable
price to resolution is 0.001 / 0.999. Sometimes the final settle is several
ticks away (especially on illiquid markets where the last trade was hours
before resolution). Empirically 0.05 is generous enough not to false-alarm
on those, but tight enough to catch real bugs.

Output
------
- ``check_all_markets`` returns a list of SanityResult records (one per market).
- ``write_sanity_report`` persists them as Parquet under
  ``data/research/eml_node/processed/_sanity_report.parquet`` for later
  analysis and cross-reference.
- ``summarize`` prints a human-readable pass/fail table.

This stage is non-destructive — it only reads + reports; it never deletes
or modifies the underlying bucketed series. Acting on failures (e.g.,
dropping bad markets from Phase 0.7's split) is a separate decision.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from polybot_research.eml_node.data.bucketing import PROCESSED_DIR
from polybot_research.eml_node.data.models import ResolvedMarket

if TYPE_CHECKING:
    import polars as pl

DEFAULT_THRESHOLD = 0.05
SANITY_REPORT_PATH = PROCESSED_DIR / "_sanity_report.parquet"


@dataclass(frozen=True)
class SanityResult:
    """Outcome of the sanity check for a single market."""

    condition_id: str
    market_id: str
    question: str
    yes_won: bool
    expected_final_yes_prob: float
    measured_final_yes_prob: float
    discrepancy: float  # absolute distance from expected
    n_real_buckets: int
    n_synthetic_buckets: int
    final_bucket_n_fills: int  # n_fills in the last real bucket
    final_bucket_usdc: float  # usdc_volume in the last real bucket
    passed: bool
    note: str = ""


# ── Pure check ────────────────────────────────────────────────────────────────


def check_market(
    market: ResolvedMarket,
    buckets: "pl.DataFrame",
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> SanityResult:
    """
    Evaluate a single market's bucketed series against its resolution.

    Final YES probability = ``yes_prob_last`` of the last bucket where
    ``is_synthetic == False``. Synthetic (forward-filled) buckets are ignored
    because they carry no new trade information about how the market actually
    settled.
    """
    if market.yes_won is None:
        return SanityResult(
            condition_id=market.condition_id,
            market_id=market.id,
            question=market.question,
            yes_won=False,
            expected_final_yes_prob=float("nan"),
            measured_final_yes_prob=float("nan"),
            discrepancy=float("nan"),
            n_real_buckets=0,
            n_synthetic_buckets=0,
            final_bucket_n_fills=0,
            final_bucket_usdc=0.0,
            passed=False,
            note="market has no parseable yes_won (outcome_prices not [0,1] or [1,0])",
        )

    if buckets.is_empty():
        return SanityResult(
            condition_id=market.condition_id,
            market_id=market.id,
            question=market.question,
            yes_won=market.yes_won,
            expected_final_yes_prob=1.0 if market.yes_won else 0.0,
            measured_final_yes_prob=float("nan"),
            discrepancy=float("nan"),
            n_real_buckets=0,
            n_synthetic_buckets=0,
            final_bucket_n_fills=0,
            final_bucket_usdc=0.0,
            passed=False,
            note="bucketed series is empty",
        )

    import polars as pl

    expected = 1.0 if market.yes_won else 0.0
    n_synthetic = int(buckets["is_synthetic"].sum())
    real = buckets.filter(~pl.col("is_synthetic")).sort("bucket_start_ts")
    n_real = real.height

    if n_real == 0:
        return SanityResult(
            condition_id=market.condition_id,
            market_id=market.id,
            question=market.question,
            yes_won=market.yes_won,
            expected_final_yes_prob=expected,
            measured_final_yes_prob=float("nan"),
            discrepancy=float("nan"),
            n_real_buckets=0,
            n_synthetic_buckets=n_synthetic,
            final_bucket_n_fills=0,
            final_bucket_usdc=0.0,
            passed=False,
            note="no real buckets (all synthetic)",
        )

    last = real.row(n_real - 1, named=True)
    measured = float(last["yes_prob_last"])
    if measured != measured:  # NaN guard
        return SanityResult(
            condition_id=market.condition_id,
            market_id=market.id,
            question=market.question,
            yes_won=market.yes_won,
            expected_final_yes_prob=expected,
            measured_final_yes_prob=float("nan"),
            discrepancy=float("nan"),
            n_real_buckets=n_real,
            n_synthetic_buckets=n_synthetic,
            final_bucket_n_fills=int(last["n_fills"]),
            final_bucket_usdc=float(last["usdc_volume"]),
            passed=False,
            note="final bucket yes_prob_last is NaN",
        )

    discrepancy = abs(measured - expected)
    passed = discrepancy <= threshold
    note = "" if passed else f"discrepancy {discrepancy:.4f} > threshold {threshold}"

    return SanityResult(
        condition_id=market.condition_id,
        market_id=market.id,
        question=market.question,
        yes_won=market.yes_won,
        expected_final_yes_prob=expected,
        measured_final_yes_prob=measured,
        discrepancy=discrepancy,
        n_real_buckets=n_real,
        n_synthetic_buckets=n_synthetic,
        final_bucket_n_fills=int(last["n_fills"]),
        final_bucket_usdc=float(last["usdc_volume"]),
        passed=passed,
        note=note,
    )


# ── Driver ────────────────────────────────────────────────────────────────────


def check_all_markets(
    kept: list[ResolvedMarket],
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[SanityResult]:
    """
    Run check_market over every kept market by reading its bucketed series
    from data/research/eml_node/processed/<condition_id>.parquet.

    Markets without a bucketed file (e.g. failed to bucket upstream) are
    reported as failures with a "missing bucket file" note rather than
    silently skipped.
    """
    import polars as pl

    results: list[SanityResult] = []
    for m in kept:
        path = PROCESSED_DIR / f"{m.condition_id}.parquet"
        if not path.exists():
            results.append(
                SanityResult(
                    condition_id=m.condition_id,
                    market_id=m.id,
                    question=m.question,
                    yes_won=bool(m.yes_won) if m.yes_won is not None else False,
                    expected_final_yes_prob=(
                        1.0 if m.yes_won else 0.0
                    ) if m.yes_won is not None else float("nan"),
                    measured_final_yes_prob=float("nan"),
                    discrepancy=float("nan"),
                    n_real_buckets=0,
                    n_synthetic_buckets=0,
                    final_bucket_n_fills=0,
                    final_bucket_usdc=0.0,
                    passed=False,
                    note=f"missing bucket file: {path.name}",
                )
            )
            continue
        df = pl.read_parquet(path)
        results.append(check_market(m, df, threshold=threshold))
    return results


# ── Reporting ────────────────────────────────────────────────────────────────


def write_sanity_report(results: list[SanityResult]) -> Path:
    """Persist results as Parquet for later analysis."""
    import polars as pl

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    df = pl.DataFrame([asdict(r) for r in results])
    df.write_parquet(SANITY_REPORT_PATH)
    return SANITY_REPORT_PATH


def summarize(results: list[SanityResult], *, top_failures: int = 5) -> dict[str, float]:
    """
    Print and return a summary of the sanity check.

    Returns a dict of high-level metrics that can be asserted against in
    higher-level validation.
    """
    n = len(results)
    if n == 0:
        logger.warning("Sanity summary: no markets to check")
        return {"n_markets": 0, "n_passed": 0, "n_failed": 0, "pass_rate": 0.0}

    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]
    discrepancies = [
        r.discrepancy for r in results
        if r.discrepancy == r.discrepancy  # exclude NaN
    ]

    metrics = {
        "n_markets": float(n),
        "n_passed": float(len(passed)),
        "n_failed": float(len(failed)),
        "pass_rate": len(passed) / n,
        "median_discrepancy": (
            sorted(discrepancies)[len(discrepancies) // 2]
            if discrepancies
            else float("nan")
        ),
        "max_discrepancy": max(discrepancies) if discrepancies else float("nan"),
    }

    logger.info("=== Phase 0.6 sanity report ===")
    logger.info("Markets checked  : {}", n)
    logger.info(
        "Passed           : {} ({:.1%})", len(passed), metrics["pass_rate"]
    )
    logger.info("Failed           : {}", len(failed))
    if discrepancies:
        logger.info(
            "Discrepancy      : median={:.4f}, max={:.4f}",
            metrics["median_discrepancy"],
            metrics["max_discrepancy"],
        )

    if failed:
        sorted_failures = sorted(
            failed,
            key=lambda r: (
                r.discrepancy if r.discrepancy == r.discrepancy else float("inf")
            ),
            reverse=True,
        )
        logger.warning("Top {} failures:", min(top_failures, len(failed)))
        for r in sorted_failures[:top_failures]:
            logger.warning(
                "  ✗ {} ({}…): expected={:.3f}, measured={:.3f}, Δ={:.3f}, "
                "fills_in_final_bucket={}, note={!r}",
                r.market_id,
                r.question[:50],
                r.expected_final_yes_prob,
                r.measured_final_yes_prob,
                r.discrepancy,
                r.final_bucket_n_fills,
                r.note,
            )

    return metrics


# ── Standalone driver (used by CLI eml-sanity) ───────────────────────────────


def run_sanity(*, threshold: float = DEFAULT_THRESHOLD) -> dict[str, float]:
    """
    Read markets_index.parquet + the raw Gamma dump, reconstruct ResolvedMarket
    objects, run the check, write the report, return summary metrics.
    """
    import json

    import polars as pl

    INTERIM = PROCESSED_DIR.parent / "interim"
    idx_path = INTERIM / "markets_index.parquet"
    raw_path = INTERIM / "gamma_markets_raw_v1.json"
    if not idx_path.exists():
        raise FileNotFoundError(
            f"{idx_path} missing. Run `uv run eml-phase0` first."
        )

    idx = pl.read_parquet(idx_path)
    kept_ids = set(idx["id"].to_list())

    raw = json.loads(raw_path.read_text())
    kept = [
        ResolvedMarket.model_validate(r) for r in raw if str(r.get("id")) in kept_ids
    ]
    logger.info("Reconstructed {} kept markets for sanity check", len(kept))

    results = check_all_markets(kept, threshold=threshold)
    write_sanity_report(results)
    metrics = summarize(results)
    logger.info("Wrote sanity report → {}", SANITY_REPORT_PATH)
    return metrics
