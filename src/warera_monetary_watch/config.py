from __future__ import annotations

from functools import lru_cache

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_base_path(value: str) -> str:
    stripped = value.strip() or "/"
    if stripped == "/":
        return "/"
    if not stripped.startswith("/"):
        stripped = f"/{stripped}"
    return stripped.rstrip("/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(alias="DATABASE_URL")
    warera_api_base_url: str = Field(alias="WARERA_API_BASE_URL")
    warera_api_token: str = Field(alias="WARERA_API_TOKEN")
    warera_api_rate_limit_per_minute: int = Field(default=450, alias="WARERA_API_RATE_LIMIT_PER_MINUTE")
    warera_api_lookup_batch_size: int = Field(default=100, alias="WARERA_API_LOOKUP_BATCH_SIZE")
    warera_api_lookup_concurrency: int = Field(default=15, alias="WARERA_API_LOOKUP_CONCURRENCY")
    app_base_path: str = Field(default="/monetary-watch", alias="APP_BASE_PATH")
    warera_host: str = Field(default="warera.xorgress.com", alias="WARERA_HOST")
    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    web_port: int = Field(default=8000, alias="WEB_PORT")
    collector_poll_interval_seconds: int = Field(default=30, alias="COLLECTOR_POLL_INTERVAL_SECONDS")
    collector_max_pages_per_cycle: int = Field(default=200, alias="COLLECTOR_MAX_PAGES_PER_CYCLE")
    collector_ingest_batch_size: int = Field(default=1000, alias="COLLECTOR_INGEST_BATCH_SIZE")
    country_metadata_refresh_seconds: int = Field(default=600, alias="COUNTRY_METADATA_REFRESH_SECONDS")
    raw_retention_days: int = Field(default=90, alias="RAW_RETENTION_DAYS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    authentik_auth_enabled: bool = Field(default=False, alias="AUTHENTIK_AUTH_ENABLED")
    authentik_base_url: str = Field(
        default="https://authentik.xorgress.com",
        alias="AUTHENTIK_BASE_URL",
    )
    authentik_client_id: str = Field(default="", alias="AUTHENTIK_CLIENT_ID")
    authentik_client_secret: str = Field(default="", alias="AUTHENTIK_CLIENT_SECRET")
    authentik_allowed_group: str = Field(
        default="warera-monetary-watch-access",
        alias="AUTHENTIK_ALLOWED_GROUP",
    )
    auth_session_secret_key: str = Field(default="", alias="AUTH_SESSION_SECRET_KEY")
    auth_session_cookie_name: str = Field(
        default="warera_monetary_watch_session",
        alias="AUTH_SESSION_COOKIE_NAME",
    )

    @computed_field
    @property
    def normalized_base_path(self) -> str:
        return normalize_base_path(self.app_base_path)

    @computed_field
    @property
    def database_sync_url(self) -> str:
        return self.database_url.replace("+asyncpg", "")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
