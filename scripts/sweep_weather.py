"""
Parameter sweep for the weather strategy.

Runs the backtest across a grid of (min_edge, sigma, hours_before) and
prints a ranked table of combinations by expected value.

Usage:
  uv run scripts/sweep_weather.py           # 30d history
  uv run scripts/sweep_weather.py 60        # 60d history (more trades, slower)
"""

import sys
import asyncio
import math
from itertools import product

sys.path.insert(0, "src")

from rich.console import Console
from rich.table import Table
from rich import box

from polybot.backtest.engine import run_backtest
import polybot.strategies.weather as _weather_mod

console = Console()

DAYS_BACK    = int(sys.argv[1]) if len(sys.argv) > 1 else 30
MIN_EDGES    = [0.05, 0.08, 0.12, 0.18]
SIGMAS       = [1.2, 1.8, 2.5, 3.5]
HOURS_BEFORE = [2, 7, 18]


def _make_ep(sigma: float):
    """Return an estimate_probability function with a fixed sigma."""
    def _ep(wq, forecast):
        mu = forecast.high_temp_c
        def phi(x):
            x = max(-50.0, min(50.0, x))
            return 1.0 / (1.0 + math.exp(-1.7 * x))
        p = phi((wq.hi - mu) / sigma) - phi((wq.lo - mu) / sigma)
        return round(max(0.01, min(0.99, p)), 4)
    return _ep


async def sweep():
    combos = list(product(MIN_EDGES, SIGMAS, HOURS_BEFORE))
    rows   = []

    console.print(
        f"\n[bold cyan]🔬 Parameter sweep — {DAYS_BACK}d history  "
        f"{len(combos)} combinations[/]\n"
    )

    orig_ep = _weather_mod.estimate_probability

    for i, (min_edge, sigma, hours) in enumerate(combos, 1):
        console.print(
            f"[grey50]  [{i}/{len(combos)}] "
            f"min_edge={min_edge:.0%}  sigma={sigma}°C  hours_before={hours}[/]",
            end="\r",
        )

        _weather_mod.estimate_probability = _make_ep(sigma)

        try:
            result = await run_backtest(
                days_back    = DAYS_BACK,
                min_edge     = min_edge,
                verbose      = False,
                hours_before = hours,
            )
        finally:
            _weather_mod.estimate_probability = orig_ep

        rows.append({
            "min_edge": min_edge,
            "sigma":    sigma,
            "hours":    hours,
            "trades":   result.total,
            "win_rate": result.win_rate,
            "ev":       result.expected_value,
            "kelly":    result.kelly_fraction,
            "avg_edge": result.avg_edge,
        })

    rows.sort(key=lambda r: r["ev"], reverse=True)

    console.print()
    t = Table(
        box=box.SIMPLE, show_header=True,
        header_style="bold bright_cyan", expand=False, padding=(0, 1),
    )
    t.add_column("MIN_EDGE",  justify="right")
    t.add_column("SIGMA(°C)", justify="right")
    t.add_column("HOURS",     justify="right")
    t.add_column("TRADES",    justify="right")
    t.add_column("WIN%",      justify="right")
    t.add_column("EV",        justify="right")
    t.add_column("KELLY",     justify="right")
    t.add_column("AVG_EDGE",  justify="right")

    for r in rows:
        ev_col  = "bright_green" if r["ev"] > 0    else "bright_red"
        k_col   = "bright_yellow" if r["kelly"] > 0 else "grey50"
        win_col = "bright_green" if r["win_rate"] >= 0.55 else "bright_red"
        t.add_row(
            f"{r['min_edge']:.0%}",
            f"{r['sigma']:.1f}",
            str(r["hours"]),
            str(r["trades"]),
            f"[{win_col}]{r['win_rate']:.1%}[/]",
            f"[{ev_col}]{r['ev']:+.4f}[/]",
            f"[{k_col}]{r['kelly']:.1%}[/]",
            f"+{r['avg_edge']:.1%}",
        )

    console.print(t)
    console.print(
        "\n[grey50]EV = expected value per $ staked. "
        "Kelly = max bankroll % per trade. "
        "Only rows with EV > 0 and Kelly > 0 are tradeable.[/]\n"
    )


asyncio.run(sweep())
