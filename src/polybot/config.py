from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, AliasChoices


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",   # silently ignore unknown .env fields
    )

    # ── Scanner ───────────────────────────────────────────────────────────────
    scan_interval_seconds: int   = Field(default=120)
    min_liquidity_usd:     float = Field(default=500.0)
    min_edge_threshold:    float = Field(default=0.08)

    # ── Simulated trading (paper mode) ─────────────────────────────────────────
    simulated_starting_balance: float = Field(
        default=1000.0,
        validation_alias=AliasChoices("simulated_starting_balance", "paper_starting_balance")
    )
    simulated_max_position_usd: float = Field(
        default=10.0,
        validation_alias=AliasChoices("simulated_max_position_usd", "paper_max_position_usd")
    )
    max_open_positions:     int   = Field(default=10)

    # ── Live execution ────────────────────────────────────────────────────────
    live_trading:         bool  = Field(default=False)
    private_key:          str   = Field(default="", validation_alias=AliasChoices("private_key", "wallet_private_key"))   # hot wallet EOA private key
    wallet_address:       str   = Field(default="")   # hot wallet EOA address
    poly_proxy_address:   str   = Field(default="")   # Polymarket proxy wallet (holds USDC)
    max_daily_loss_usd:   float = Field(default=50.0)
    live_max_position_usd: float = Field(default=50.0)
    poly_key_id:          str   = Field(default="")
    poly_secret_key:      str   = Field(default="")
    
    # Relayer API key (gasless onchain ops — from polymarket.com/settings)
    relayer_api_key:  str = Field(default="")
    relayer_address:  str = Field(default="")

    # CLOB API credentials (order placement — from create_or_derive_api_creds())
    clob_api_key:        str = Field(default="")
    clob_api_secret:     str = Field(default="")
    clob_api_passphrase: str = Field(default="")

    # Legacy field name aliases
    secret_key:  str = Field(default="")   # some scripts used this
    passphrase:  str = Field(default="")   # some scripts used this

    # ── Web dashboard ─────────────────────────────────────────────────────────
    web_enabled: bool = Field(default=True)
    web_host:    str  = Field(default="0.0.0.0")
    web_port:    int  = Field(default=8765)

    # ── Telegram (optional) ───────────────────────────────────────────────────
    telegram_bot_token: str = Field(default="")
    telegram_chat_id:   int = Field(default=0)

    # ── Crypto bot ────────────────────────────────────────────────────────────
    crypto_enabled:  bool  = Field(default=False)
    crypto_min_edge: float = Field(default=0.10)

    # ── Polymarket US (sports bot) ────────────────────────────────────────────
    # From polymarket.us/developer — completely separate from global CLOB keys.
    polymarket_key_id:     str  = Field(default="")
    polymarket_secret_key: str  = Field(default="")
    sports_enabled:        bool = Field(default=False)
    sports_scan_interval_seconds: int = Field(default=30)
    sports_min_edge:       float = Field(default=0.05)   # 5¢ min for sports
    sports_max_daily_loss: float = Field(default=50.0)

    # ── Live in-game trading ──────────────────────────────────────────────────
    live_sports_enabled:               bool  = Field(default=False)
    live_sports_min_edge:              float = Field(default=0.08)
    live_sports_min_seconds_remaining: float = Field(default=120.0)   # 2-min floor
    live_sports_blowout_margin:        float = Field(default=0.85)    # skip/exit above this
    live_sports_kelly_fraction:        float = Field(default=0.15)
    live_sports_max_position_usd:      float = Field(default=8.0)
    espn_live_poll_interval:           int   = Field(default=30)      # cache TTL in seconds

    # ── The Odds API (sports confirmation — Layer 2) ───────────────────────────
    odds_api_key: str = Field(default="")

    # ── Delay Arbitrage ─────────────────────────────────────────────────────────
    # Gated by default — enable only after testing. Safe rollout toggle.
    delay_arb_enabled: bool = Field(default=False)
    delay_arb_cooldown_minutes: float = Field(default=30.0)

    # ── Paths ─────────────────────────────────────────────────────────────────
    trade_log_path:   str = Field(default="data/trades/trades.jsonl")
    log_file_path:    str = Field(default="data/trades/bot.log")
    weather_log_path: str = Field(default="data/trades/weather.log")
    sports_log_path:  str = Field(default="data/trades/sports.log")

    # ── Headless / service mode ───────────────────────────────────────────────
    # Set HEADLESS=true on VPS to skip the Rich terminal renderer.
    # The scanner still runs and publishes state over WebSocket.
    headless: bool = Field(default=False)

    # ── Dashboard service ─────────────────────────────────────────────────────
    # URL the dashboard service uses to consume state from the weather bot.
    scanner_ws_url:   str = Field(default="ws://localhost:8765/ws")
    dashboard_host:   str = Field(default="0.0.0.0")
    dashboard_port:   int = Field(default=8766)

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO")


settings = Settings()