"""
Diagnostic for the `price` field semantics on OrderFilledEvent.

Run after `uv run eml-phase0` (with raw amounts now captured). Compares the
subgraph's reported `price` against several derived candidates so we can pin
down what `price` actually means.

Usage:
    uv run python scripts/inspect_fills.py
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

FILLS_DIR = Path("data/research/eml_node/interim/fills")
INDEX_PATH = Path("data/research/eml_node/interim/markets_index.parquet")


def main() -> None:
    idx = pl.read_parquet(INDEX_PATH)
    yes_token_ids = set(idx["clob_token_id_yes"].drop_nulls().to_list())
    no_token_ids = set(idx["clob_token_id_no"].drop_nulls().to_list())

    print("=== `price` semantics check ===\n")

    files = sorted(FILLS_DIR.glob("*.parquet"))
    for f in files[:4]:  # first four assets is enough to see the pattern
        df = pl.read_parquet(f)
        if df.is_empty():
            continue

        # Side under test: the asset_id this file is keyed on
        # (file naming convention from pipeline._to_fills_path).
        asset_id_prefix = f.name.split("__")[0]
        is_yes = any(tok.startswith(asset_id_prefix) for tok in yes_token_ids)
        is_no = any(tok.startswith(asset_id_prefix) for tok in no_token_ids)
        side_label = "YES" if is_yes else "NO" if is_no else "?"

        # Find the matching market for context
        if is_yes:
            market_row = idx.filter(
                pl.col("clob_token_id_yes").str.starts_with(asset_id_prefix)
            )
        else:
            market_row = idx.filter(
                pl.col("clob_token_id_no").str.starts_with(asset_id_prefix)
            )
        question = market_row["question"][0] if market_row.height else "?"
        yes_won = market_row["yes_won"][0] if market_row.height else None

        # Candidate price interpretations
        df = df.with_columns(
            [
                (pl.col("taker_amount_filled") / pl.col("maker_amount_filled")).alias(
                    "ratio_taker_over_maker"
                ),
                (pl.col("maker_amount_filled") / pl.col("taker_amount_filled")).alias(
                    "ratio_maker_over_taker"
                ),
            ]
        )

        print(f"── {f.name} ── ({side_label} token of: {question[:60]})")
        print(f"   resolved: yes_won={yes_won}")
        print(f"   fills: {df.height}")
        print(
            f"   subgraph price : min={df['price'].min():.4f} "
            f"median={df['price'].median():.4f} max={df['price'].max():.4f}"
        )
        print(
            f"   taker / maker  : min={df['ratio_taker_over_maker'].min():.4f} "
            f"median={df['ratio_taker_over_maker'].median():.4f} "
            f"max={df['ratio_taker_over_maker'].max():.4f}"
        )
        print(
            f"   maker / taker  : min={df['ratio_maker_over_taker'].min():.4f} "
            f"median={df['ratio_maker_over_taker'].median():.4f} "
            f"max={df['ratio_maker_over_taker'].max():.4f}"
        )

        # Last 3 fills with side and ratios
        print("   last 3 fills (timestamp, side, price, taker/maker, maker/taker):")
        tail = df.tail(3).select(
            ["timestamp", "side", "price", "ratio_taker_over_maker", "ratio_maker_over_taker"]
        )
        for row in tail.iter_rows(named=True):
            print(
                f"     ts={row['timestamp']} side={row['side']:5s} "
                f"price={row['price']:.4f} t/m={row['ratio_taker_over_maker']:.4f} "
                f"m/t={row['ratio_maker_over_taker']:.4f}"
            )
        print()

    print(
        "Interpretation cheat-sheet:\n"
        "  - If subgraph `price` matches taker/maker for one side and maker/taker\n"
        "    for the other, then `price` is in token-per-USDC units (i.e. 1/probability).\n"
        "  - If it matches maker/taker uniformly, `price` is the inverse of probability.\n"
        "  - If it matches taker/maker uniformly and stays in [0,1], `price` IS\n"
        "    the implied probability (and our earlier reading was wrong).\n"
        "  - If none match cleanly, the field has side-dependent semantics that\n"
        "    need to be derived per-row from `side` + which asset you queried.\n"
    )


if __name__ == "__main__":
    main()
