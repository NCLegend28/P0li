from polybot.trading.engine import TradingEngine as PaperTrader

# Backward-compat alias: PaperTrader now points to the new TradingEngine
# This shim ensures existing imports like `from polybot.paper.trader import PaperTrader` keep working.
PaperTrader  # noqa: F401
