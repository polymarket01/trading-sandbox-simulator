from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Spot Market Sandbox"
    app_env: str = "development"
    api_prefix: str = "/api/v1"
    database_url: str = "postgresql+asyncpg://sandbox:sandbox@db:5432/sandbox"
    redis_url: str = "redis://redis:6379/0"
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    default_manual_api_key: str = "manual-demo-key"
    default_admin_api_key: str = "admin-demo-key"
    default_ws_signature_ttl_ms: int = 60_000
    depth_levels: int = 20
    stats_push_interval_ms: int = 1000
    recent_trade_limit: int = 200


settings = Settings()
