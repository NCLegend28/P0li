from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from polybot.api.coingecko import CoinData
from polybot.api.openmeteo import CityForecast
from polybot.models import Market, Opportunity, TradeRecord
from polybot.strategies.exit import ExitSignal


class ScanState(BaseModel):
    """
    Immutable-by-convention state flowing through the scanner graph.
    Each node returns a dict that gets merged into this state object.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Phase 1 — raw markets from Gamma
    raw_markets: list[Market] = Field(default_factory=list)

    # Phase 2 — markets that survive structural filters
    filtered_markets: list[Market] = Field(default_factory=list)

    # Phase 3 — new opportunities from strategies
    opportunities: list[Opportunity] = Field(default_factory=list)

    # Phase 4 — exit signals for existing open positions
    exit_signals: list[ExitSignal] = Field(default_factory=list)

    # Injected by CLI before each run — open positions for exit monitoring
    open_positions: list[TradeRecord] = Field(default_factory=list)

    # Built by fetch_forecasts, consumed by run_strategies and monitor_positions
    forecast_cache: dict[str, CityForecast] = Field(default_factory=dict)

    # Built by fetch_crypto_prices, consumed by run_strategies
    coin_cache: dict[str, CoinData] = Field(default_factory=dict)

    # Metadata
    scan_number: int = 0
    errors: list[str] = Field(default_factory=list)
