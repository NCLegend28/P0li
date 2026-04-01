"""
Sports strategy backtest.

Usage:
  uv run scripts/backtest_sports.py [days_back] [min_edge] [hours_before]

Examples:
  uv run scripts/backtest_sports.py               # 30d, 5% edge, 6h before
  uv run scripts/backtest_sports.py 60            # 60 days of history
  uv run scripts/backtest_sports.py 30 0.08       # 8% minimum signal
  uv run scripts/backtest_sports.py 30 0.05 4     # enter 4h before resolution
"""

import sys
import asyncio

sys.path.insert(0, "src")

from polybot.backtest.sports_engine import main

days  = int(sys.argv[1])   if len(sys.argv) > 1 else 30
edge  = float(sys.argv[2]) if len(sys.argv) > 2 else 0.05
hours = int(sys.argv[3])   if len(sys.argv) > 3 else 6

asyncio.run(main(days_back=days, min_edge=edge, hours_before=hours))
