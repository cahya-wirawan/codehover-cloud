"""Process configuration from environment variables (22_DEPLOYMENT_AND_OPERATIONS.md).

Secrets are `SecretStr` so they never appear in reprs, logs or error output. Production rules are
enforced at startup rather than discovered later.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
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
    # Public repositories without the App: an optional token (no permissions needed) only raises GitHub's
    # metadata rate limit for the whole server; source downloads are always anonymous.
    github_public_token: SecretStr | None = None

    # Provider credentials (07): a provider's catalog models are usable only when its key is set.
    anthropic_api_key: SecretStr | None = None
    # Optional operator endpoint for calls with ANTHROPIC_API_KEY (a gateway or proxy): an https origin with
    # an optional path. Unset means the official API. Workspaces' own keys always use the official API.
    anthropic_base_url: str | None = None
    openai_api_key: SecretStr | None = None
    # The same for calls with OPENAI_API_KEY: an https endpoint serving the Responses API (default
    # https://api.openai.com/v1). Workspaces' own OpenAI keys always use the official API.
    openai_base_url: str | None = None
    gemini_api_key: SecretStr | None = None  # the Gemini API (generativelanguage.googleapis.com), not Vertex
    openrouter_api_key: SecretStr | None = None
    # Approved catalog file (07, 23); without it only the zero-cost replay preset exists.
    model_catalog_file: str | None = None
    # Generation allowance of each workspace per calendar month until entitlements exist (11, M4/M5).
    generation_allowance_micro_usd: int = Field(default=0, ge=0)
    max_active_builds_per_workspace: int = Field(default=2, ge=1, le=20)
    max_active_acquisitions_per_workspace: int = Field(default=2, ge=1, le=20)
    # Rate limits shared by all replicas (06 "Initial rate policies"; app/core/ratelimit.py).
    rate_limit_user_per_minute: int = Field(default=120, ge=1)
    rate_limit_signin_per_minute: int = Field(default=10, ge=1)
    rate_limit_public_per_minute: int = Field(default=300, ge=1)
    rate_limit_estimates_per_hour: int = Field(default=10, ge=1)
    rate_limit_acquisitions_per_hour: int = Field(default=10, ge=1)
    rate_limit_builds_per_day: int = Field(default=20, ge=1)
    rate_limit_billing_per_hour: int = Field(default=20, ge=1)
    # Push rebuilds (M6): pushes within the debounce window coalesce; a request runs at most max-wait
    # after its first push.
    push_debounce_seconds: int = Field(default=60, ge=0, le=3600)
    push_max_wait_seconds: int = Field(default=600, ge=0, le=86_400)
    # Comma-separated networks whose X-Forwarded-For is believed (the ingress proxy only, 22).
    trusted_proxies: str = ""
    # Internal Prometheus port per process (12); 0 disables it. Never routed by the gateway.
    metrics_port: int = Field(default=0, ge=0, le=65535)
    # SCIP indexers (T23): the pinned Node binary and the directory holding the installed indexer packages
    # (the backend image sets both). Unset keeps every reference on name matching.
    scip_node_path: str | None = None
    scip_tools_dir: str | None = None
    scip_timeout_seconds: int = Field(default=300, ge=10, le=1800)
    scip_heap_mb: int = Field(default=1536, ge=256, le=8192)
    # rust-analyzer's binary and Rust's library sources (rust-src); unset keeps Rust on name matching.
    scip_rust_analyzer_path: str | None = None
    scip_rust_src_dir: str | None = None

    stripe_secret_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None
    # Stripe price IDs per plan key (11): only configured plans can be bought.
    stripe_price_pro: str | None = None
    stripe_price_team: str | None = None
    # Paid launch: workspaces need a plan (or trial) to create projects and invite members (11). Off for the beta.
    billing_enforced: bool = False
    # Shown on the support, privacy and terms pages.
    operator_name: str = "codehover-cloud"
    support_email: str | None = None

    def configured_providers(self) -> frozenset[str]:
        """Providers whose catalog models can run: replay always, others only with credentials."""
        keys = {
            "anthropic": self.anthropic_api_key,
            "openai": self.openai_api_key,
            "gemini": self.gemini_api_key,
            "openrouter": self.openrouter_api_key,
        }
        return frozenset({"replay"} | {provider for provider, key in keys.items() if key is not None})

    @field_validator(
        "migration_database_url",
        "amqp_url",
        "rabbitmq_management_url",
        "google_client_id",
        "google_client_secret",
        "github_oauth_client_id",
        "github_oauth_client_secret",
        "github_app_id",
        "github_app_slug",
        "github_app_client_id",
        "github_app_client_secret",
        "github_app_private_key_file",
        "github_webhook_secret",
        "github_public_token",
        "anthropic_api_key",
        "anthropic_base_url",
        "openai_api_key",
        "openai_base_url",
        "gemini_api_key",
        "openrouter_api_key",
        "model_catalog_file",
        "stripe_secret_key",
        "stripe_webhook_secret",
        "stripe_price_pro",
        "stripe_price_team",
        "support_email",
        "scip_node_path",
        "scip_tools_dir",
        "scip_rust_analyzer_path",
        "scip_rust_src_dir",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """`KEY=` in an env file means "not configured", never an empty credential."""
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, SecretStr) and not value.get_secret_value().strip():
            return None
        return value

    @field_validator("trusted_proxies")
    @classmethod
    def _networks(cls, value: str) -> str:
        import ipaddress

        for network in (n.strip() for n in value.split(",")):
            if network:
                ipaddress.ip_network(network, strict=False)  # raises on anything but an address or CIDR
        return value

    @field_validator("public_origin")
    @classmethod
    def _origin_only(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("PUBLIC_ORIGIN must be an absolute http(s) origin")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("PUBLIC_ORIGIN must be an origin without path, query or fragment")
        return f"{parts.scheme}://{parts.netloc}"

    @field_validator("anthropic_base_url", "openai_base_url")
    @classmethod
    def _https_endpoint(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        name = (info.field_name or "").upper()
        parts = urlsplit(value.strip())
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError(f"{name} must be an https URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError(f"{name} must not contain credentials, a query or a fragment")
        return value.strip().rstrip("/")

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
