from __future__ import annotations

import httpx
import pytest
from fastapi import APIRouter
from pydantic import BaseModel

from app import __version__
from app.core.settings import Settings
from app.main import API_PREFIX, create_app


def client_for(settings: Settings, extra: APIRouter | None = None) -> httpx.AsyncClient:
    app = create_app(settings)
    if extra is not None:
        app.include_router(extra, prefix=API_PREFIX)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_live_reports_version_and_request_id(settings: Settings) -> None:
    async with client_for(settings) as client:
        response = await client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}
    assert len(response.headers["x-request-id"]) == 32


async def test_valid_upstream_request_id_is_kept_and_invalid_is_replaced(settings: Settings) -> None:
    async with client_for(settings) as client:
        kept = await client.get("/api/v1/health/live", headers={"x-request-id": "abc-123-def-456"})
        replaced = await client.get("/api/v1/health/live", headers={"x-request-id": "bad id\r\n"})
    assert kept.headers["x-request-id"] == "abc-123-def-456"
    assert replaced.headers["x-request-id"] != "bad id\r\n"
    assert len(replaced.headers["x-request-id"]) == 32


async def test_ready_is_503_envelope_when_database_unreachable(settings: Settings) -> None:
    async with client_for(settings) as client:
        response = await client.get("/api/v1/health/ready", headers={"x-request-id": "ready-check-0001"})
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "NOT_READY"
    assert error["retryable"] is True
    assert error["request_id"] == "ready-check-0001"
    assert error["details"] == {"failed": ["database"]}
    assert "127.0.0.1" not in response.text  # connection details never reach clients


async def test_unknown_route_uses_envelope(settings: Settings) -> None:
    async with client_for(settings) as client:
        response = await client.get("/api/v1/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


class Payload(BaseModel):
    name: str
    count: int


async def test_validation_error_does_not_echo_input(settings: Settings) -> None:
    router = APIRouter()

    @router.post("/echo")
    async def echo(payload: Payload) -> Payload:
        return payload

    async with client_for(settings, router) as client:
        response = await client.post("/api/v1/echo", json={"name": "secret-value-xyz", "count": "many"})
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "VALIDATION_FAILED"
    assert body["details"]["fields"] == [{"location": ["body", "count"], "type": "int_parsing"}]
    assert "secret-value-xyz" not in response.text and "many" not in response.text


async def test_unexpected_exception_is_generic_500(settings: Settings) -> None:
    router = APIRouter()

    @router.get("/boom")
    async def boom() -> None:
        raise RuntimeError("internal detail: password=hunter2")

    async with client_for(settings, router) as client:
        response = await client.get("/api/v1/boom")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL"
    assert "hunter2" not in response.text


@pytest.mark.parametrize("path", ["/api/v1/openapi.json", "/api/v1/docs"])
async def test_docs_hidden_in_production_only_for_docs(path: str) -> None:
    from app.core.settings import Environment
    from tests.conftest import make_settings

    prod = make_settings(
        app_env=Environment.PRODUCTION,
        public_origin="https://example.test",
        s3_endpoint_url="https://s3.example.test",  # production refuses to start without object storage
    )
    async with client_for(prod) as client:
        response = await client.get(path)
    assert response.status_code == (200 if path.endswith("openapi.json") else 404)
