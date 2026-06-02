# EML × Neural ODE × Polymarket

Research project — train an EML-parameterized Neural ODE on Polymarket
LMSR-implied-probability time series, attempt symbolic recovery of the
right-hand-side dynamics, and backtest any recovered formulas as falsifiable
trading hypotheses.

**Full plan, milestones, risk register, success criteria** live in the vault:

```
Vault of Knowledge/wiki/projects/eml-neural-ode-polymarket.md
Vault of Knowledge/wiki/insights/eml-as-ml-substrate.md
Vault of Knowledge/wiki/concepts/eml-operator.md
Vault of Knowledge/wiki/concepts/symbolic-regression.md
```

This README is the *implementation* index. It points at code paths, not
research framing.

---

## Phase status

| Phase | Substeps | Status | Module(s) |
|---|---|---|---|
| 0.0 | Schema introspection | ✅ implemented | `data/schema_check.py` |
| 0.1 | Gamma `/markets` access | ✅ verified | `data/gamma.py` |
| 0.1.5 | `outcomePrices` semantics | ✅ verified | (see vault) |
| 0.2 | Pull resolved-market universe | ✅ implemented | `data/pipeline.py:discover_markets` |
| 0.3 | Persist markets index | ✅ implemented | `data/pipeline.py:write_markets_index` |
| 0.4 | Pull fills from subgraph | ✅ implemented | `data/pipeline.py:pull_fills_for_kept_markets` |
| 0.5 | Bucket fills → time series | ✅ implemented | `data/bucketing.py` |
| 0.6 | Sanity cross-check | ✅ implemented | `data/sanity.py` |
| 0.7 | Train/dev/held-out split | ⏳ TODO | (next) |
| 1 | Baselines (NODE, PySR, EWMA) | ⏳ TODO | |
| 2 | EML primitive + verification | 🟡 stub | `eml/operator.py` |
| 3 | EML-RHS Neural ODE | 🟡 stub | `node/` |
| 4 | Symbolic snap + inspect | ⏳ TODO | |
| 5 | Falsifiability backtest | ⏳ TODO | |
| 6 | (gated) Productize | ⏳ TODO | |

---

## Layout

```
src/polybot_research/eml_node/
├── README.md         ← this file
├── __init__.py
├── cli.py            ← console_scripts entry points
├── data/             ← Phase 0 — data acquisition, filtering, bucketing
│   ├── models.py        ResolvedMarket, Fill, MarketFillSeries pydantic models
│   ├── filter.py        Audit-ready client-side filter for Gamma markets
│   ├── gamma.py         ResolvedMarketsClient — Gamma /markets paginator
│   ├── subgraph.py      SubgraphClient — Graph subgraph GraphQL client
│   ├── probability.py   Implied YES-probability derivation from fills
│   ├── bucketing.py     Phase 0.5 — per-market YES-prob time series
│   ├── sanity.py        Phase 0.6 — bucketed final price vs Gamma resolution
│   ├── schema_check.py  Phase 0.0 schema introspection + snapshot diff
│   └── pipeline.py      Phase 0 orchestrator (run_phase_0)
├── eml/              ← Phase 2 — EML primitive (mostly stubs)
│   └── operator.py      eml() function + EMLNode/EMLTree stubs
└── node/             ← Phase 3 — EML-RHS Neural ODE (stub)
```

Outputs and snapshots:

```
data/research/eml_node/
├── schema_snapshots/    ← JSON snapshots; diffed by schema_check.py
├── interim/             ← Pre-bucketing artifacts (raw Gamma dump, fills, index)
├── processed/           ← Bucketed time series (Phase 0.5+)
└── raw/                 ← Reserved for any other raw data
```

---

## Setup (one-time)

```bash
cd ~/projects/polymarket-bot

# Install with research extras (torch, torchdiffeq, polars, pyarrow, mlflow, ...)
uv sync --extra research

# Add Graph API key to .env (NOT inline anywhere)
echo 'GRAPH_API_KEY=your_actual_key_here' >> .env
```

> **Python version pin.** This project is pinned to **Python 3.12** via
> `.python-version` in the repo root. Several research deps (`pyarrow`, `torch`,
> `mlflow`) ship prebuilt wheels for 3.12/3.13 only; on 3.14 `uv sync --extra
> research` falls back to source builds and `pyarrow` requires Apache Arrow C++
> headers that aren't installed. `uv` reads `.python-version` and grabs a 3.12
> interpreter automatically. The live bot also satisfies the project's
> `requires-python = ">=3.12"`, so this pin doesn't affect it.

---

## Running

```bash
# Phase 0.0 — verify subgraph schema (run first on every new session)
uv run eml-schema-check

# Phases 0.2–0.6 — discover, filter, pull fills, bucket, sanity-check
uv run eml-phase0

# Re-run just the Phase 0.6 sanity check against existing on-disk data
# (exits non-zero if any markets fail)
uv run eml-sanity

# Quick "what's on disk?" report (includes sanity pass/fail counts)
uv run eml-status
```

The first `eml-phase0` run with default `max_markets=50` is intentionally
small. Once the filter behavior is empirically verified, bump it up in
`pipeline.run_phase_0`.

---

## Reused from `polybot`

This package deliberately reuses existing infrastructure rather than duplicate
it:

- `polybot.utils.retry.async_retry` — used in `data/gamma.py` and
  `data/subgraph.py`
- `polybot.config.Settings` — research code reads from the same `.env`
  (just adds `GRAPH_API_KEY`)
- Pydantic patterns from `polybot.models` (separate model class because the
  schema needs are different — `ResolvedMarket` carries resolution payouts,
  condition IDs, AMM/CLOB era flags, none of which the live model has)

It does NOT touch:

- `polybot.api.gamma.GammaClient` (live-bot has it hardcoded to open markets;
  we run our own variant for closed markets)
- `polybot.scanner.graph` (LangGraph orchestrator for the live bot — not a
  GraphQL subgraph client, despite the name)
- Any strategy modules

---

## Anti-pattern guard

This project deliberately holds the line against the engagement-bait
"Polymarket bot" attention loop named in `wiki/self/patterns.md`. If a phase
slips into "did the bot make money?" framing without the falsifiability
gate, that's a process bug. The Phase 5 backtest report is where the
hypothesis gets tested — until that report is honest, the project is in
research, not trading.
