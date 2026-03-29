# scripts/setup_clob.py
import os
from dotenv import load_dotenv
from py_clob_client.client import ClobClient

load_dotenv()   # ← this reads .env into os.environ

pk = os.getenv("PRIVATE_KEY")
if not pk:
    raise ValueError("PRIVATE_KEY not found — check your .env file")

print(f"Using key: {pk[:6]}...{pk[-4:]}")  # shows first/last chars only, never full key

client = ClobClient(
    host     = "https://clob.polymarket.com",
    chain_id = 137,
    key      = pk,
)

creds = client.create_or_derive_api_creds()

print("\nAdd these to your .env:\n")
print(f"CLOB_API_KEY={creds.api_key}")
print(f"CLOB_API_SECRET={creds.api_secret}")
print(f"CLOB_API_PASSPHRASE={creds.api_passphrase}")