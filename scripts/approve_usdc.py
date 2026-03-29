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
usdc      = float(bal.get("balance",    0)) / 1e6
allowance = float(bal.get("allowance",  0)) / 1e6
print(f"  Balance:   ${usdc:,.2f}")
print(f"  Allowance: ${allowance:,.2f}")

if allowance > 0:
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
    allowance2 = float(bal2.get("allowance", 0)) / 1e6
    print(f"  New allowance: ${allowance2:,.2f}")
    print("\n✓ Done — bot can now place live orders.")