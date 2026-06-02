"""Smoke-test the forecast cache: second pass should not hit the network."""
from __future__ import annotations

import asyncio
import time

from polybot.api.openmeteo import OpenMeteoClient, _FORECAST_CACHE


CITIES = ["MIAMI", "CHICAGO", "LONDON", "TOKYO", "SAN FRANCISCO"]


async def main() -> None:
    async with OpenMeteoClient() as meteo:
        t0 = time.monotonic()
        for c in CITIES:
            fc = await meteo.fetch_forecast(c)
            print(f"  first  {c:<16} high={fc.high_temp_c:.1f}°C")
        first_dur = time.monotonic() - t0

        t1 = time.monotonic()
        for c in CITIES:
            fc = await meteo.fetch_forecast(c)
            print(f"  second {c:<16} high={fc.high_temp_c:.1f}°C")
        second_dur = time.monotonic() - t1

    print(f"\nFirst pass:  {first_dur:.2f}s  ({len(CITIES)} live fetches)")
    print(f"Second pass: {second_dur:.4f}s  (should be ≪ first pass — cache hits)")
    print(f"Cache size:  {len(_FORECAST_CACHE)} entries")
    assert second_dur < first_dur / 5, "Cache did not short-circuit the second pass"
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
