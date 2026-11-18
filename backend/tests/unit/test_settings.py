from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from app.core.settings import Environment
from tests.conftest import make_settings


def test_secrets_are_not_in_repr() -> None:
    settings = make_settings(model_api_key=SecretStr("sk-very-secret"))
    assert "sk-very-secret" not in repr(settings)
    assert "s" * 32 not in repr(settings)


def test_public_origin_is_normalized_and_must_be_an_origin() -> None:
    assert (
        make_settings(public_origin="https://app.example.test/").public_origin == "https://app.example.test"
    )
    with pytest.raises(ValidationError):
        make_settings(public_origin="https://app.example.test/path")
    with pytest.raises(ValidationError):
        make_settings(public_origin="javascript:alert(1)")


def test_database_url_requires_psycopg_driver() -> None:
    with pytest.raises(ValidationError):
        make_settings(database_url=SecretStr("postgresql://u:p@db/x"))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"public_origin": "http://app.example.test"}, "https"),
        ({"session_cookie_secure": False}, "SESSION_COOKIE_SECURE"),
        ({"session_secret": SecretStr("short")}, "SESSION_SECRET"),
        ({"app_encryption_key": SecretStr("s" * 32)}, "must differ"),
    ],
)
def test_production_rules(overrides: dict[str, object], message: str) -> None:
    base: dict[str, object] = {"app_env": Environment.PRODUCTION, "public_origin": "https://app.example.test"}
    base.update(overrides)
    with pytest.raises(ValidationError, match=message):
        make_settings(**base)


def test_valid_production_settings() -> None:
    settings = make_settings(app_env=Environment.PRODUCTION, public_origin="https://app.example.test")
    assert settings.session_cookie_secure is True


def test_json_log_lines_carry_request_id_and_drop_uvicorn_color_field() -> None:
    import json
    import logging

    from app.core.logging import JsonFormatter
    from app.core.request_context import request_id_var

    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, "Started", None, None)
    record.color_message = "\x1b[36mStarted\x1b[0m"
    record.role = "api"
    token = request_id_var.set("req-abc12345")
    try:
        line = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert line["msg"] == "Started" and line["request_id"] == "req-abc12345" and line["role"] == "api"
    assert "color_message" not in line


def test_configure_logging_silences_http_client_url_logging() -> None:
    import logging

    from app.core.logging import configure_logging

    configure_logging("INFO")
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpcore").isEnabledFor(logging.INFO)
    assert logging.getLogger("app").isEnabledFor(logging.INFO)


def test_production_app_refuses_to_start_without_object_storage() -> None:
    from app.main import create_app

    with pytest.raises(RuntimeError, match="S3 storage must be configured"):
        create_app(make_settings(app_env=Environment.PRODUCTION, public_origin="https://app.example.test"))
