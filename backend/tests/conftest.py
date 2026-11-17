from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.core.settings import Environment, Settings


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": Environment.TEST,
        "database_url": SecretStr("postgresql+psycopg://nobody@127.0.0.1:1/none"),
        "session_secret": SecretStr("s" * 32),
        "app_encryption_key": SecretStr("k" * 32),
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _isolate_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not depend on the developer's shell (e.g. a sourced deployment/.env)."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def settings() -> Settings:
    return make_settings()
