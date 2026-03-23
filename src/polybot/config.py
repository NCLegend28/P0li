from __future__ import annotations

import warnings
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Polymarket
    wallet_private_key: str = Field(default="", description="Hot wallet private key")

    # Scanner
    scan_interval_seconds: int = Field(default=120)
    min_liquidity_usd: float = Field(default=500.0)
    min_edge_threshold: float = Field(default=0.08)

    # Paper trading
    paper_starting_balance: float = Field(default=1000.0)
    paper_max_position_usd: float = Field(default=10.0)
    max_open_positions: int = Field(default=10)

    # Web dashboard
    web_enabled: bool = Field(default=True)
    web_host:    str  = Field(default="0.0.0.0")
    web_port:    int  = Field(default=8765)

    # Telegram (optional — bot won't start if these are empty)
    telegram_bot_token: str      = Field(default="")
    telegram_chat_id:   int      = Field(default=0)

    # Logging
    log_level: str = Field(default="INFO")

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("min_edge_threshold")
    @classmethod
    def check_min_edge(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"min_edge_threshold must be 0.0–1.0, got {v}")
        return v

    @field_validator("scan_interval_seconds")
    @classmethod
    def check_scan_interval(cls, v: int) -> int:
        if v < 10:
            raise ValueError(f"scan_interval_seconds must be >= 10, got {v}")
        return v

    @field_validator("paper_max_position_usd")
    @classmethod
    def check_max_position(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"paper_max_position_usd must be > 0, got {v}")
        return v

    @field_validator("wallet_private_key")
    @classmethod
    def warn_if_empty_key(cls, v: str) -> str:
        if not v:
            warnings.warn(
                "wallet_private_key is empty — live trading will not work",
                stacklevel=2,
            )
        return v

    @field_validator("telegram_chat_id")
    @classmethod
    def warn_if_zero_chat_id(cls, v: int) -> int:
        if v == 0:
            warnings.warn(
                "telegram_chat_id is 0 — Telegram alerts will be disabled",
                stacklevel=2,
            )
        return v

    # ── Absolute path helpers ─────────────────────────────────────────────────

    @property
    def project_root(self) -> Path:
        """Absolute path to the project root (two levels above src/polybot)."""
        return Path(__file__).resolve().parents[2]

    @property
    def data_dir(self) -> Path:
        return self.project_root / "data"

    @property
    def trade_log_path(self) -> Path:
        return self.data_dir / "trades" / "paper_trades.jsonl"

    @property
    def log_file_path(self) -> Path:
        return self.data_dir / "trades" / "bot.log"


settings = Settings()
