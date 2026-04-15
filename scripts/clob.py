from dotenv import load_dotenv
load_dotenv()

import os
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, PartialCreateOrderOptions

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

# ── Replace this with a real token ID before running ─────────────────────────
# Find token IDs via: https://clob.polymarket.com/markets or by running:
#   uv run python -c "from polybot.api.clob_client import ClobClient; ..."
# A token ID looks like a 64-char hex string, NOT "123456".
TOKEN_ID = os.getenv("TEST_TOKEN_ID", "")
if not TOKEN_ID or TOKEN_ID == "123456":
    raise SystemExit(
        "Set TEST_TOKEN_ID in .env to a real CLOB token ID before running this script."
    )

order = client.create_and_post_order(
    OrderArgs(token_id=TOKEN_ID, price=0.65, size=100, side="BUY"),
    PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
)