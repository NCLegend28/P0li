from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


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

    # ── Simulated trading (paper mode) ────────────────────────────────────────
    simulated_starting_balance: float = Field(default=1000.0)
    simulated_max_position_usd: float = Field(default=10.0)
    max_open_positions:         int   = Field(default=10)

    # ── Cheap-tail size boost ─────────────────────────────────────────────────
    # Audit (2026-05-18) showed the 0.00–0.20 entry-price bucket is the only
    # profitable cohort. When the bot enters at or below `cheap_tail_threshold`,
    # the per-position cap is multiplied by `cheap_tail_size_multiplier` so
    # Kelly sizing isn't truncated. Set the multiplier to 1.0 to disable.
    cheap_tail_threshold:        float = Field(default=0.20)
    cheap_tail_size_multiplier:  float = Field(default=2.5)

    # ── Live execution (Polymarket global / CLOB) ─────────────────────────────
    live_trading:         bool  = Field(default=False)
    wallet_private_key:   str   = Field(default="")   # hot wallet EOA private key
    poly_proxy_address:   str   = Field(default="")   # Polymarket proxy wallet (holds USDC)
    max_daily_loss_usd:   float = Field(default=50.0)
    live_max_position_usd: float = Field(default=50.0)

    # CLOB API credentials (order placement — from create_or_derive_api_creds())
    clob_api_key:        str = Field(default="")
    clob_api_secret:     str = Field(default="")
    clob_api_passphrase: str = Field(default="")

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
    sports_min_edge:       float = Field(default=0.05)
    sports_max_daily_loss: float = Field(default=50.0)

    # ── Sports v1 (alert-only, flat sizing, scope filter) ─────────────────────
    # Comma-separated league codes the scanner is allowed to surface.
    # Recognised codes: NBA, MLB, NHL, NFL, EPL, UCL, MLS, FIFA, WNBA, UFC.
    sports_leagues:        str   = Field(default="NBA,MLB,EPL,UCL,MLS,FIFA")
    # Hard kill-switch for sports auto-execution. True = scanner finds + alerts,
    # never places live US orders even if polymarket_key_id is configured.
    sports_alert_only:     bool  = Field(default=True)
    # Flat dollar size for every sports opportunity (overrides Kelly).
    # Set to 0 to fall back to Kelly sizing.
    sports_flat_size_usd:  float = Field(default=20.0)

    # ── Vault integration (Obsidian sports knowledge base) ────────────────────
    # Absolute path to the Obsidian vault root. Empty string disables the
    # vault adapter — the bot still runs fine without it.
    vault_path:            str   = Field(default="")
    vault_cache_seconds:   int   = Field(default=600)

    # ── Live in-game trading ──────────────────────────────────────────────────
    live_sports_enabled:               bool  = Field(default=False)
    live_sports_min_edge:              float = Field(default=0.08)
    live_sports_min_seconds_remaining: float = Field(default=120.0)
    live_sports_blowout_margin:        float = Field(default=0.85)
    live_sports_kelly_fraction:        float = Field(default=0.15)
    live_sports_max_position_usd:      float = Field(default=8.0)
    espn_live_poll_interval:           int   = Field(default=30)

    # ── The Odds API (sports confirmation — Layer 2) ──────────────────────────
    odds_api_key: str = Field(default="")

    # ── Delay Arbitrage ───────────────────────────────────────────────────────
    delay_arb_enabled:          bool  = Field(default=False)
    delay_arb_cooldown_minutes: float = Field(default=30.0)

    # ── Paths ─────────────────────────────────────────────────────────────────
    trade_log_path:   str = Field(default="data/trades/trades.jsonl")
    log_file_path:    str = Field(default="data/trades/bot.log")
    weather_log_path: str = Field(default="data/trades/weather.log")
    sports_log_path:  str = Field(default="data/trades/sports.log")

    # ── Headless / service mode ───────────────────────────────────────────────
    # Set HEADLESS=true on VPS to skip the Rich terminal renderer.
    headless: bool = Field(default=False)

    # ── Dashboard service ─────────────────────────────────────────────────────
    scanner_ws_url:   str = Field(default="ws://localhost:8765/ws")
    dashboard_host:   str = Field(default="0.0.0.0")
    dashboard_port:   int = Field(default=8766)

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO")

    # ── LLM opportunity picker (optional) ─────────────────────────────────────
    # OpenAI-compatible endpoint: Together AI, Fireworks, vLLM, local llama.cpp.
    llm_picker_enabled: bool = Field(default=False)
    llm_base_url:       str  = Field(default="https://api.together.xyz/v1")
    llm_model:          str  = Field(default="writer/palmyra-fin-70b-32k")
    llm_api_key:        str  = Field(default="")

    @property
    def sports_league_set(self) -> frozenset[str]:
        """Parsed `sports_leagues` as an uppercased frozenset (e.g. {"NBA","MLB"})."""
        return frozenset(
            code.strip().upper()
            for code in self.sports_leagues.split(",")
            if code.strip()
        )


settings = Settings()
