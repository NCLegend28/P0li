"""
One-time setup: tells the Polymarket Exchange contract
it's allowed to spend USDC from your proxy wallet.
Must be run before the bot can place any live orders.
"""
import os
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
    signature_type = 2,
    funder         = os.getenv("POLY_PROXY_ADDRESS"),
)

# Check current allowance first
print("Current USDC allowance...")
bal = client.get_balance_allowance(
    params=BalanceAllowanceParams(
        asset_type     = AssetType.COLLATERAL,
        signature_type = 2,
    )
)
usdc      = float(bal.get("balance", 0)) / 1e6
# API returns "allowances" (dict of contract -> amount), not "allowance"
allowances = bal.get("allowances", {})
max_allowance = max((int(v) for v in allowances.values()), default=0)
print(f"  Balance:   ${usdc:,.2f}")
print(f"  Approved contracts: {len(allowances)}")
for addr, amt in allowances.items():
    label = "unlimited" if int(amt) > 10**30 else f"${int(amt)/1e6:,.2f}"
    print(f"    {addr}: {label}")

if max_allowance > 0:
    print("\n✓ Allowance already set — nothing to do.")
else:
    print("\nSetting USDC allowance for Exchange contract...")
    result = client.update_balance_allowance(
        params=BalanceAllowanceParams(
            asset_type     = AssetType.COLLATERAL,
            signature_type = 2,
        )
    )
    print(f"  Result: {result}")

    # Verify
    bal2      = client.get_balance_allowance(
        params=BalanceAllowanceParams(
            asset_type     = AssetType.COLLATERAL,
            signature_type = 2,
        )
    )
    allowances2 = bal2.get("allowances", {})
    max2 = max((int(v) for v in allowances2.values()), default=0)
    label2 = "unlimited" if max2 > 10**30 else f"${max2/1e6:,.2f}"
    print(f"  New allowance: {label2}")
    print("\n✓ Done — bot can now place live orders.")