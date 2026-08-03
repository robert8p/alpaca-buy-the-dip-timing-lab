from __future__ import annotations

import os
from dataclasses import dataclass, field


def _csv_ints(value: str, default: list[int]) -> list[int]:
    if not value.strip():
        return default
    return [int(x.strip()) for x in value.split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))
    alpaca_api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    alpaca_secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
    alpaca_feed: str = field(default_factory=lambda: os.getenv("ALPACA_FEED", "sip").lower())
    app_username: str = field(default_factory=lambda: os.getenv("APP_USERNAME", "admin"))
    app_password: str = field(default_factory=lambda: os.getenv("APP_PASSWORD", ""))
    session_secret: str = field(default_factory=lambda: os.getenv("SESSION_SECRET", ""))
    session_cookie_secure: bool = field(default_factory=lambda: os.getenv("SESSION_COOKIE_SECURE", "true").lower() not in {"0", "false", "no"})
    worker_poll_seconds: int = field(default_factory=lambda: int(os.getenv("WORKER_POLL_SECONDS", "5")))
    stale_work_minutes: int = field(default_factory=lambda: int(os.getenv("STALE_WORK_MINUTES", "15")))
    alpaca_market_data_rpm: int = field(default_factory=lambda: int(os.getenv("ALPACA_MARKET_DATA_RPM", "60")))
    request_timeout_seconds: int = field(default_factory=lambda: int(os.getenv("REQUEST_TIMEOUT_SECONDS", "45")))
    database_pool_max: int = field(default_factory=lambda: int(os.getenv("DATABASE_POOL_MAX", "3")))
    max_request_retries: int = field(default_factory=lambda: int(os.getenv("MAX_REQUEST_RETRIES", "6")))
    default_cost_bps: list[int] = field(default_factory=lambda: _csv_ints(os.getenv("DEFAULT_COST_BPS", "15,20,50"), [15, 20, 50]))
    bootstrap_iterations: int = field(default_factory=lambda: int(os.getenv("BOOTSTRAP_ITERATIONS", "2000")))

    @property
    def alpaca_headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.alpaca_api_key,
            "APCA-API-SECRET-KEY": self.alpaca_secret_key,
        }

    def validate_web(self) -> None:
        missing: list[str] = []
        if not self.database_url:
            missing.append("DATABASE_URL")
        if not self.app_password or len(self.app_password) < 12:
            missing.append("APP_PASSWORD (minimum 12 characters)")
        if not self.session_secret or len(self.session_secret) < 32:
            missing.append("SESSION_SECRET (minimum 32 characters)")
        if missing:
            raise RuntimeError("Missing or insecure web settings: " + ", ".join(missing))

    def validate_worker(self) -> None:
        missing: list[str] = []
        if not self.database_url:
            missing.append("DATABASE_URL")
        if not self.alpaca_api_key:
            missing.append("ALPACA_API_KEY")
        if not self.alpaca_secret_key:
            missing.append("ALPACA_SECRET_KEY")
        if self.alpaca_feed not in {"sip", "iex"}:
            missing.append("ALPACA_FEED must be sip or iex")
        if missing:
            raise RuntimeError("Missing or invalid worker settings: " + ", ".join(missing))


settings = Settings()
