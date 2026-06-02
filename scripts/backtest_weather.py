"""
Weather strategy backtest.

Usage:
  uv run scripts/backtest_weather.py [days_back] [min_edge] [types]

Examples:
  uv run scripts/backtest_weather.py               # 14d, 8% edge, all types
  uv run scripts/backtest_weather.py 30            # 30 days of history
  uv run scripts/backtest_weather.py 30 0.06       # 6% minimum edge
  uv run scripts/backtest_weather.py 60 0.08 exact # exact-temperature markets only
"""

import sys
import asyncio

sys.path.insert(0, "src")

from polybot.backtest.engine import main

days  = int(sys.argv[1])                          if len(sys.argv) > 1 else 14
edge  = float(sys.argv[2])                        if len(sys.argv) > 2 else 0.08
types = [t.strip() for t in sys.argv[3].split(",")] if len(sys.argv) > 3 else None

asyncio.run(main(days_back=days, min_edge=edge, question_types=types))
