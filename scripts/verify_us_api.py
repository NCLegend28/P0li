#!/usr/bin/env python
"""
Verify Polymarket US API credentials and connectivity.

Usage:
    uv run scripts/verify_us_api.py

Checks:
  1. SDK is installed
  2. Auth: can reach the US API with configured keys
  3. Data: fetches active events and prints the top 5
  4. Book: fetches order book for the first available market
  5. Account: fetches balance (requires configured keys)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add project root to path so polybot package is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _check_env() -> tuple[str, str]:
    from polybot.config import settings
    key_id = settings.polymarket_key_id
    secret = settings.polymarket_secret_key
    if not key_id or not secret:
        print("\n[FAIL] POLYMARKET_KEY_ID and/or POLYMARKET_SECRET_KEY not set in .env")
        print("       Get credentials at: polymarket.us/developer")
        sys.exit(1)
    print(f"[OK]   Credentials loaded (key_id={key_id[:8]}...)")
    return key_id, secret


def _check_sdk() -> None:
    try:
        import polymarket_us  # noqa: F401
        print("[OK]   polymarket-us SDK installed")
    except ImportError:
        print("[FAIL] polymarket-us SDK not installed. Run: uv add polymarket-us")
        sys.exit(1)


def _check_sync(key_id: str, secret: str) -> str | None:
    """Run sync checks — auth, events, book."""
    from polybot.api.polymarket_us import PolymarketUSClient

    print("\n── Sync client ──────────────────────────────────────────────────")
    client = PolymarketUSClient(key_id=key_id, secret_key=secret)

    # Account balance
    try:
        balance = client.get_balance()
        print(f"[OK]   Balance: {balance}")
    except Exception as e:
        print(f"[WARN] Could not fetch balance: {e}")

    # Active events
    first_slug = None
    try:
        result = client.list_events(limit=5, active=True)
        events = result.get("events", []) if isinstance(result, dict) else result
        print(f"[OK]   Active events: {len(events)} returned")
        for ev in events[:5]:
            title = ev.get("title", ev.get("name", "(no title)"))
            markets = ev.get("markets", [ev])
            print(f"         • {title} — {len(markets)} market(s)")
            if not first_slug and markets:
                first_slug = markets[0].get("slug", markets[0].get("id", ""))
    except Exception as e:
        print(f"[FAIL] Could not fetch events: {e}")

    # Order book
    if first_slug:
        try:
            book = client.get_bbo(first_slug)
            print(f"[OK]   BBO for '{first_slug}': {book}")
        except Exception as e:
            print(f"[WARN] Could not fetch BBO for '{first_slug}': {e}")

    client.close()
    return first_slug


async def _check_async(key_id: str, secret: str) -> None:
    """Run async checks — list markets, search."""
    from polybot.api.polymarket_us import AsyncPolymarketUSClient

    print("\n── Async client ─────────────────────────────────────────────────")
    client = AsyncPolymarketUSClient(key_id=key_id, secret_key=secret)
    try:
        result = await client.list_markets(limit=5)
        markets = result.get("markets", []) if isinstance(result, dict) else result
        print(f"[OK]   list_markets: {len(markets)} markets returned")

        result = await client.search("NBA")
        hits = result.get("markets", []) if isinstance(result, dict) else result
        print(f"[OK]   search('NBA'): {len(hits)} results")
    except Exception as e:
        print(f"[FAIL] Async client error: {e}")
    finally:
        await client.close()


def _check_odds() -> None:
    from polybot.config import settings
    if not settings.odds_api_key:
        print("\n[SKIP] ODDS_API_KEY not set — skipping Odds API check")
        return

    print("\n── Odds API (Layer 2 confirmation) ──────────────────────────────")

    async def _run():
        from polybot.api.odds import OddsClient
        client = OddsClient(api_key=settings.odds_api_key)
        games = await client.fetch_odds("NBA")
        if games:
            g = games[0]
            print(f"[OK]   {g.home_team} vs {g.away_team}: home_prob={g.home_prob:.3f}")
            print(f"       {len(games)} total NBA games fetched")
        else:
            print("[WARN] No NBA games returned (off-season or key issue?)")

    asyncio.run(_run())


def _check_espn() -> None:
    print("\n── ESPN schedule/injuries ───────────────────────────────────────")

    async def _run():
        from polybot.api.espn import ESPNClient
        espn = ESPNClient()
        games = await espn.fetch_schedule("NBA")
        injuries = await espn.fetch_injuries("NBA")
        print(f"[OK]   ESPN NBA today: {len(games)} games, {len(injuries)} injuries")
        for g in games[:3]:
            print(f"         • {g.away_team} @ {g.home_team}  [{g.status}]")

    asyncio.run(_run())


def main() -> None:
    print("=" * 60)
    print("  Polymarket US API Verification")
    print("=" * 60)

    _check_sdk()
    key_id, secret = _check_env()
    _check_sync(key_id, secret)
    asyncio.run(_check_async(key_id, secret))
    _check_odds()
    _check_espn()

    print("\n" + "=" * 60)
    print("  All checks complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
