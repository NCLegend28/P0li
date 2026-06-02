import os
from typing import Any, cast
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

load_dotenv()

creds = ApiCreds(
    api_key        = os.getenv("CLOB_API_KEY"),
    api_secret     = os.getenv("CLOB_API_SECRET"),
    api_passphrase = os.getenv("CLOB_API_PASSPHRASE"),
)

client = ClobClient(
    host           = "https://clob.polymarket.com",
    chain_id       = 137,
    key            = os.getenv("WALLET_PRIVATE_KEY"),
    creds          = creds,
    signature_type = 2,                          # GNOSIS_SAFE proxy wallet
    funder         = os.getenv("POLY_PROXY_ADDRESS"),
)

print("Checking API credentials...")
ok = client.get_ok()
print(f"  CLOB reachable:   {ok}")

print("Checking USDC balance...")
bal = cast(Any, client.get_balance_allowance(
    params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
))
usdc = float(bal.get("balance", 0)) / 1e6
# py-clob-client now returns an `allowances` map keyed by exchange/spender
# contract, not a single `allowance` field. Treat any positive spender
# allowance as usable; old clients may still return `allowance`.
if "allowances" in bal:
    allowance = max(float(v) for v in bal.get("allowances", {}).values()) / 1e6
else:
    allowance = float(bal.get("allowance", 0)) / 1e6
print(f"  USDC balance:     ${usdc:,.2f}")
print(f"  USDC allowance:   ${allowance:,.2f}")
if allowance == 0:
    print("  ⚠️  Allowance is 0 — run scripts/approve_usdc.py before live trading")

print("Checking markets...")
markets = client.get_markets()
data = markets.get("data", [])
print(f"  Markets fetched:  {len(data)}")

print("\n✓ All good — credentials verified.")