"""
setup_relayer.py — One-time setup using the Polymarket relayer.

Steps:
  1. Deploy your Safe proxy wallet (if not already deployed)
  2. Approve USDC.e for the Exchange contract (so Polymarket can settle trades)

Run once before live trading. Safe to run again — it checks before deploying.

Usage:
  python scripts/setup_relayer.py
"""

import os
from dotenv import load_dotenv

from py_builder_relayer_client.client import RelayClient
from py_builder_relayer_client.models import SafeTransaction, OperationType
from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds

load_dotenv()

# ── Polygon mainnet contract addresses ──────────────────────────────────────
USDC_E   = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"   # USDC.e (bridged)
EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"   # Polymarket Exchange
CTF      = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"   # CTF contract

# ── ERC-20 approve(spender, amount) calldata ────────────────────────────────
# approve(address,uint256) selector = 0x095ea7b3
# amount = uint256 max (0xfff...fff = unlimited)
MAX_UINT256 = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"

def encode_approve(spender: str) -> str:
    """Encode ERC-20 approve(spender, uint256_max) calldata."""
    # selector: approve(address,uint256)
    selector = "095ea7b3"
    # ABI-encode address (32 bytes, left-padded with zeros)
    spender_padded = spender.lower().replace("0x", "").zfill(64)
    amount_padded  = MAX_UINT256
    return "0x" + selector + spender_padded + amount_padded


def main():
    pk      = os.getenv("WALLET_PRIVATE_KEY")
    api_key = os.getenv("POLY_BUILDER_API_KEY")
    secret  = os.getenv("POLY_BUILDER_SECRET")
    passph  = os.getenv("POLY_BUILDER_PASSPHRASE")

    if not pk:
        raise ValueError("WALLET_PRIVATE_KEY not set in .env")
    if not api_key:
        raise ValueError("POLY_BUILDER_API_KEY not set in .env")

    # ── Build client ──────────────────────────────────────────────────────────
    builder_config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key        = api_key,
            secret     = secret,
            passphrase = passph,
        )
    )

    client = RelayClient(
        relayer_url    = "https://relayer-v2.polymarket.com",
        chain_id       = 137,
        private_key    = pk,
        builder_config = builder_config,
    )

    safe_address = client.get_expected_safe()
    print(f"\nSafe (proxy) wallet address: {safe_address}")
    print("This is your POLY_PROXY_ADDRESS — add it to .env if not already there.\n")

    # ── Step 1: Deploy Safe if needed ─────────────────────────────────────────
    deployed = client.get_deployed(safe_address)
    if deployed:
        print("✓ Safe already deployed — skipping deployment.")
    else:
        print("Deploying Safe wallet (gasless — Polymarket pays)...")
        response = client.deploy()
        result   = response.wait()
        state    = result.get("state") if result else "STATE_FAILED"
        print(f"  State: {state}")
        if state in ("STATE_CONFIRMED", "STATE_MINED"):
            print(f"  ✓ Safe deployed at {safe_address}")
        else:
            print(f"  ✗ Deployment failed: {result}")
            return

    # ── Step 2: Approve USDC.e for Exchange contract ──────────────────────────
    print("\nApproving USDC.e for Exchange contract (gasless)...")

    approve_tx = SafeTransaction(
        to        = USDC_E,
        operation = OperationType.Call,
        data      = encode_approve(EXCHANGE),
        value     = "0",
    )

    response = client.execute([approve_tx], metadata="Approve USDC.e for Exchange")
    result   = response.wait()
    state    = result.get("state") if result else "STATE_FAILED"
    print(f"  State: {state}")

    if state in ("STATE_CONFIRMED", "STATE_MINED"):
        print("  ✓ USDC.e approved for Exchange contract")
    else:
        print(f"  ✗ Approval failed: {result}")
        return

    # ── Step 3: Approve USDC.e for CTF contract ───────────────────────────────
    print("\nApproving USDC.e for CTF contract (gasless)...")

    approve_ctf_tx = SafeTransaction(
        to        = USDC_E,
        operation = OperationType.Call,
        data      = encode_approve(CTF),
        value     = "0",
    )

    response = client.execute([approve_ctf_tx], metadata="Approve USDC.e for CTF")
    result   = response.wait()
    state    = result.get("state") if result else "STATE_FAILED"
    print(f"  State: {state}")

    if state in ("STATE_CONFIRMED", "STATE_MINED"):
        print("  ✓ USDC.e approved for CTF contract")
    else:
        print(f"  ✗ CTF approval failed: {result}")
        return

    print("\n" + "="*50)
    print("✓ Setup complete. Your .env should have:")
    print(f"  POLY_PROXY_ADDRESS={safe_address}")
    print("="*50)
    print("\nNext: send USDC.e to the proxy address above, then flip LIVE_TRADING=true")


if __name__ == "__main__":
    main()