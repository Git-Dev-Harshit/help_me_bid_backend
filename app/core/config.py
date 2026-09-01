"""Application settings.

Every environment-specific value lives here and is sourced from environment
variables (or a local ``.env``).  Nothing else in the codebase reads
``os.environ`` directly - that keeps configuration discoverable and testable.
"""

from __future__ import annotations

import zoneinfo
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

AppEnv = Literal["development", "staging", "production"]
RawRetentionMode = Literal["always", "on_failure", "never"]
NotificationProviderName = Literal["log", "fcm", "webpush"]

INSECURE_JWT_SECRETS = {
    "change-me",
    "change-me-please-generate-a-real-secret",
    "change-this-secret",
    "secret",
}


class Settings(BaseSettings):
    """Typed, validated application configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ------------------------------------------------------
    app_env: AppEnv = "development"
    app_name: str = "IPO Tracker"
    app_timezone: str = "Asia/Kolkata"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    docs_enabled: bool | None = None
    api_v1_prefix: str = "/api/v1"

    # --- Database ---------------------------------------------------------
    database_url: str = "postgresql+asyncpg://ipo:ipo_password@postgres:5432/ipo_tracker"
    db_pool_size: Annotated[int, Field(ge=1, le=50)] = 5
    db_max_overflow: Annotated[int, Field(ge=0, le=50)] = 10
    db_pool_timeout: Annotated[int, Field(ge=1)] = 30
    db_pool_recycle: Annotated[int, Field(ge=60)] = 1800
    db_echo: bool = False

    # --- Security ---------------------------------------------------------
    jwt_secret_key: str = "change-me-please-generate-a-real-secret"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: Annotated[int, Field(ge=1)] = 30
    jwt_issuer: str = "ipo-tracker"
    default_phone_region: str = "IN"
    password_min_length: Annotated[int, Field(ge=6, le=128)] = 8

    # --- CORS -------------------------------------------------------------
    # NoDecode: pydantic-settings would otherwise try to JSON-parse this before
    # the validator below runs, which breaks the comma-separated .env form.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    cors_allow_credentials: bool = True

    # --- Rate limiting ----------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_auth_per_minute: Annotated[int, Field(ge=1)] = 10

    # --- Scraper ----------------------------------------------------------
    scraper_enabled: bool = True
    # Window start times ("HH:MM", in APP_TIMEZONE) for the daily scrapes. Each
    # run fires at a uniformly random moment inside
    # [start, start + SCRAPER_SCHEDULE_JITTER_MINUTES), re-rolled every day, so
    # the request pattern is not perfectly periodic.
    # Leave empty to fall back to fixed-interval scraping.
    scraper_schedule_times: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["09:00", "14:00", "20:00"]
    )
    scraper_schedule_jitter_minutes: Annotated[int, Field(ge=0, le=720)] = 30
    # Only used when scraper_schedule_times is empty.
    scraper_interval_minutes: Annotated[int, Field(ge=1)] = 30
    # With only a few runs per day, one failure leaves data stale for hours, so
    # a failed run schedules a single retry this many minutes later (0 = off).
    scraper_failure_retry_minutes: Annotated[int, Field(ge=0, le=240)] = 20
    # Scrape once at start-up, but only when there is no IPO data yet. A fresh
    # install is populated immediately without re-scraping on every restart.
    scraper_run_on_startup_if_empty: bool = True
    scraper_report_id: int = 331
    scraper_page_url: str = "https://www.investorgain.com/report/ipo-gmp-live/331/"
    scraper_api_base_url: str = "https://webnodejs.investorgain.com"
    scraper_timeout_seconds: Annotated[int, Field(ge=1)] = 30
    scraper_max_retries: Annotated[int, Field(ge=0, le=10)] = 3
    scraper_retry_backoff_seconds: Annotated[float, Field(ge=0)] = 2.0
    scraper_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    scraper_min_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5
    scraper_raw_retention_mode: RawRetentionMode = "on_failure"
    scraper_raw_retention_days: Annotated[int, Field(ge=0)] = 14
    scraper_snapshots_enabled: bool = True

    # --- Notifications ----------------------------------------------------
    notification_enabled: bool = True
    notification_interval_minutes: Annotated[int, Field(ge=1)] = 15
    notification_provider: NotificationProviderName = "log"
    notification_window_start_hour: Annotated[int, Field(ge=0, le=23)] = 8
    notification_window_end_hour: Annotated[int, Field(ge=1, le=24)] = 22
    notification_max_age_minutes: Annotated[int, Field(ge=1)] = 60

    # --- Optional provider credentials ------------------------------------
    fcm_credentials_file: str | None = None
    fcm_project_id: str | None = None
    vapid_public_key: str | None = None
    vapid_private_key: str | None = None
    vapid_subject: str = "mailto:admin@example.com"

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------
    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        """Accept a comma-separated string, which is how .env files carry lists."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("scraper_schedule_times", mode="before")
    @classmethod
    def _split_schedule_times(cls, value: object) -> object:
        """Accept a comma-separated string, which is how .env files carry lists."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("scraper_schedule_times")
    @classmethod
    def _validate_schedule_times(cls, value: list[str]) -> list[str]:
        """Require ``HH:MM`` and normalise (``9:5`` -> ``09:05``)."""
        normalized: list[str] = []
        for raw in value:
            hour, _, minute = raw.partition(":")
            try:
                hour_int, minute_int = int(hour), int(minute)
            except ValueError as exc:
                raise ValueError(
                    f"SCRAPER_SCHEDULE_TIMES entry {raw!r} must look like HH:MM"
                ) from exc
            if not (0 <= hour_int <= 23 and 0 <= minute_int <= 59):
                raise ValueError(f"SCRAPER_SCHEDULE_TIMES entry {raw!r} is not a valid time")
            normalized.append(f"{hour_int:02d}:{minute_int:02d}")
        return normalized

    @field_validator("app_timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            zoneinfo.ZoneInfo(value)
        except Exception as exc:  # pragma: no cover - defensive
            raise ValueError("Unknown APP_TIMEZONE: " + value) from exc
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Invalid LOG_LEVEL: " + value)
        return level

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL must use the asyncpg driver, e.g. "
                "postgresql+asyncpg://user:pass@host:5432/dbname"
            )
        return value

    @model_validator(mode="after")
    def _validate_production_hardening(self) -> Settings:
        if self.notification_window_end_hour <= self.notification_window_start_hour:
            raise ValueError(
                "NOTIFICATION_WINDOW_END_HOUR must be greater than "
                "NOTIFICATION_WINDOW_START_HOUR"
            )
        if self.app_env == "production":
            if self.jwt_secret_key in INSECURE_JWT_SECRETS or len(self.jwt_secret_key) < 32:
                raise ValueError(
                    "JWT_SECRET_KEY must be a unique value of at least 32 characters "
                    "when APP_ENV=production. Generate one with: openssl rand -hex 32"
                )
            if "*" in self.cors_origins:
                raise ValueError("CORS_ORIGINS must not be a wildcard when APP_ENV=production")
        return self

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------
    @property
    def timezone(self) -> zoneinfo.ZoneInfo:
        """Business timezone used for all IPO date comparisons."""
        return zoneinfo.ZoneInfo(self.app_timezone)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def expose_docs(self) -> bool:
        """Docs default to on everywhere except production."""
        if self.docs_enabled is not None:
            return self.docs_enabled
        return not self.is_production

    @property
    def docs_url(self) -> str | None:
        return "/docs" if self.expose_docs else None

    @property
    def redoc_url(self) -> str | None:
        return "/redoc" if self.expose_docs else None

    @property
    def openapi_url(self) -> str | None:
        return "/openapi.json" if self.expose_docs else None

    @property
    def sync_database_url(self) -> str:
        """Sync URL - Alembic runs its migrations synchronously."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://")

    @property
    def scrape_windows(self) -> list[tuple[int, int]]:
        """Configured scrape times as ``(hour, minute)`` pairs."""
        return [
            (int(entry[:2]), int(entry[3:]))
            for entry in self.scraper_schedule_times
        ]

    @property
    def uses_scheduled_scrape_times(self) -> bool:
        """True when scraping runs at fixed daily times rather than an interval."""
        return bool(self.scraper_schedule_times)

    @property
    def maintenance_database_url(self) -> str:
        """URL of the ``postgres`` maintenance database on the same server.

        Used only to create the application database when it does not exist yet,
        which is what keeps start-up free of manual SQL.
        """
        base, _, _ = self.database_url.rpartition("/")
        return f"{base}/postgres"

    @property
    def database_name(self) -> str:
        """Database name from DATABASE_URL, ignoring any query string."""
        return self.database_url.rpartition("/")[2].split("?")[0]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


settings = get_settings()
