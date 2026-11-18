"""Process configuration from environment variables (22_DEPLOYMENT_AND_OPERATIONS.md).

Secrets are `SecretStr` so they never appear in reprs, logs or error output. Production rules are
enforced at startup rather than discovered later.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_LENGTH = 32


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    app_env: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"

    # Database: runtime role for API/workers, app_system for control loops, DDL role for migrations.
    database_url: SecretStr
    migration_database_url: SecretStr | None = None
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    db_pool_timeout_seconds: float = Field(default=5.0, gt=0)

    amqp_url: SecretStr | None = None
    rabbitmq_management_url: str | None = None

    s3_endpoint_url: str | None = None
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_bucket: str = "codehover"
    s3_region: str = "us-east-1"

    public_origin: str = "http://localhost:8080"
    session_cookie_secure: bool = True
    session_secret: SecretStr
    app_encryption_key: SecretStr

    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: SecretStr | None = None
    github_app_id: str | None = None
    github_app_slug: str | None = None
    github_app_client_id: str | None = None
    github_app_client_secret: SecretStr | None = None
    github_app_private_key_file: str | None = None
    github_webhook_secret: SecretStr | None = None

    # Provider credentials (07): a provider's catalog models are usable only when its key is set.
    anthropic_api_key: SecretStr | None = None
    # Approved catalog file (07, 23); without it only the zero-cost replay preset exists.
    model_catalog_file: str | None = None
    # Generation allowance of each workspace per calendar month until entitlements exist (11, M4/M5).
    generation_allowance_micro_usd: int = Field(default=0, ge=0)
    max_active_builds_per_workspace: int = Field(default=2, ge=1, le=20)
    max_active_acquisitions_per_workspace: int = Field(default=2, ge=1, le=20)
    # Internal Prometheus port per process (12); 0 disables it. Never routed by the gateway.
    metrics_port: int = Field(default=0, ge=0, le=65535)

    stripe_secret_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None

    def configured_providers(self) -> frozenset[str]:
        """Providers whose catalog models can run: replay always, others only with credentials."""
        return frozenset({"replay"} | ({"anthropic"} if self.anthropic_api_key is not None else set()))

    @field_validator("public_origin")
    @classmethod
    def _origin_only(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("PUBLIC_ORIGIN must be an absolute http(s) origin")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("PUBLIC_ORIGIN must be an origin without path, query or fragment")
        return f"{parts.scheme}://{parts.netloc}"

    @field_validator("database_url", "migration_database_url")
    @classmethod
    def _psycopg_driver(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("database URLs must use the postgresql+psycopg:// driver")
        return value

    @model_validator(mode="after")
    def _production_rules(self) -> Self:
        if self.app_env is not Environment.PRODUCTION:
            return self
        problems = []
        if not self.public_origin.startswith("https://"):
            problems.append("PUBLIC_ORIGIN must be https in production")
        if not self.session_cookie_secure:
            problems.append("SESSION_COOKIE_SECURE must be true in production")
        for name in ("session_secret", "app_encryption_key"):
            secret: SecretStr = getattr(self, name)
            if len(secret.get_secret_value()) < MIN_SECRET_LENGTH:
                problems.append(f"{name.upper()} must be at least {MIN_SECRET_LENGTH} characters")
        if self.session_secret.get_secret_value() == self.app_encryption_key.get_secret_value():
            problems.append("SESSION_SECRET and APP_ENCRYPTION_KEY must differ")
        if problems:
            raise ValueError("; ".join(problems))
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # required fields come from the environment
