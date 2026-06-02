"""
EML-parameterized Neural ODE on Polymarket LMSR-implied-probability time series.

See:
- This package's README.md for layout and how to run each phase.
- The vault project page for full plan, milestones, risk register:
    Vault of Knowledge/wiki/projects/eml-neural-ode-polymarket.md
- The motivating insight:
    Vault of Knowledge/wiki/insights/eml-as-ml-substrate.md

Phase status (as of scaffold time):
- Phase 0 (data acquisition): IMPLEMENTED — see polybot_research.eml_node.data
- Phase 1 (baselines):        TODO
- Phase 2 (EML primitive):    STUB — see polybot_research.eml_node.eml
- Phase 3 (EML-RHS Neural ODE): TODO — see polybot_research.eml_node.node
- Phases 4-6: TODO
"""

from polybot_research.eml_node.data.models import (
    Fill,
    MarketFillSeries,
    ResolvedMarket,
)

__all__ = ["Fill", "MarketFillSeries", "ResolvedMarket"]
