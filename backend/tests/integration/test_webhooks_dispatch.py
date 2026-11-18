"""GitHub webhooks (signature, dedupe, installation lifecycle) and outbox dispatch (T05/T07)."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.db import session_factory
from app.jobs import control
from tests.integration.auth_support import ddl
from tests.integration.conftest import SYSTEM_URL, needs_db, needs_system

pytestmark = [pytest.mark.integration, needs_db]
SECRET = "webhook-test-secret"


def signed(
    client: httpx.AsyncClient,
    body: dict[str, Any],
    *,
    delivery: str | None = None,
    secret: str = SECRET,
    event: str = "installation",
) -> Any:
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        "/api/v1/webhooks/github",
        content=raw,
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery or secrets.token_hex(8),
            "Content-Type": "application/json",
        },
    )


@pytest.fixture
def hooks(client: httpx.AsyncClient) -> httpx.AsyncClient:
    app = client.app  # type: ignore[attr-defined]
    app.state.settings = app.state.settings.model_copy(update={"github_webhook_secret": SecretStr(SECRET)})
    return client


def connection_with_installation(status: str = "active") -> tuple[int, str]:
    installation = secrets.randbelow(10**9) + 1
    run = secrets.token_hex(4)
    (user,) = ddl("INSERT INTO users(display_name) VALUES ('w') RETURNING id")[0]
    (ws,) = ddl("INSERT INTO workspaces(name, slug) VALUES ('Hooks', :s) RETURNING id", s=f"h-{run}")[0]
    ddl(
        "INSERT INTO repository_connections(workspace_id, installation_id, github_account_id, github_account_login,"
        " connected_by, status) VALUES (:w, :i, 1, 'org', :u, :s)",
        w=ws,
        i=installation,
        u=user,
        s=status,
    )
    return installation, str(ws)


def status_of(installation: int) -> str:
    return str(
        ddl("SELECT status FROM repository_connections WHERE installation_id = :i", i=installation)[0][0]
    )


async def test_signature_is_required(hooks: httpx.AsyncClient) -> None:
    installation, _ = connection_with_installation()
    forged = await signed(hooks, {"action": "deleted", "installation": {"id": installation}}, secret="wrong")
    assert forged.status_code == 401 and status_of(installation) == "active"


async def test_installation_lifecycle_and_dedupe(hooks: httpx.AsyncClient) -> None:
    installation, _ = connection_with_installation()
    delivery = secrets.token_hex(8)
    suspend = await signed(
        hooks, {"action": "suspend", "installation": {"id": installation}}, delivery=delivery
    )
    assert suspend.json() == {"state": "processed"} and status_of(installation) == "suspended"
    replay = await signed(
        hooks, {"action": "unsuspend", "installation": {"id": installation}}, delivery=delivery
    )
    assert replay.json() == {"duplicate": True} and status_of(installation) == "suspended"
    await signed(hooks, {"action": "deleted", "installation": {"id": installation}})
    assert status_of(installation) == "revoked"
    revive = await signed(hooks, {"action": "unsuspend", "installation": {"id": installation}})
    assert revive.status_code == 202 and status_of(installation) == "revoked"  # never revived by a webhook


async def test_unrelated_events_are_recorded_and_ignored(hooks: httpx.AsyncClient) -> None:
    response = await signed(hooks, {"zen": "Keep it logically awesome."}, event="ping")
    assert response.json() == {"state": "ignored"}


class FakeExchange:
    def __init__(self, fail: bool = False) -> None:
        self.messages: list[bytes] = []
        self.fail = fail

    async def publish(self, message: Any, routing_key: str, mandatory: bool) -> None:
        if self.fail:
            raise ConnectionError("broker down")
        assert routing_key == "task" and mandatory
        self.messages.append(message.body)


@needs_system
async def test_dispatcher_publishes_and_backs_off() -> None:
    _, ws = connection_with_installation()
    payload = {
        "schema_version": 1,
        "task_id": "00000000-0000-7000-8000-000000000001",
        "build_id": "00000000-0000-7000-8000-000000000002",
        "workspace_id": ws,
        "trace_id": "a" * 32,
    }
    (row,) = ddl(
        "INSERT INTO outbox(workspace_id, aggregate_id, event_type, payload)"
        " VALUES (:w, :a, 'task.ready', CAST(:p AS jsonb)) RETURNING id",
        w=ws,
        a=payload["task_id"],
        p=json.dumps(payload),
    )[0]
    assert SYSTEM_URL
    sessions = session_factory(create_async_engine(SYSTEM_URL))
    down = FakeExchange(fail=True)
    async with sessions() as db:
        assert await control.dispatch_once(db, down) == 0
    attempts, published = ddl("SELECT dispatch_attempts, published_at FROM outbox WHERE id = :i", i=row)[0]
    assert attempts == 1 and published is None  # kept for retry, not lost
    ddl("UPDATE outbox SET available_at = now() WHERE id = :i", i=row)
    up = FakeExchange()
    async with sessions() as db:
        assert await control.dispatch_once(db, up) >= 1
    assert any(json.loads(m)["task_id"] == payload["task_id"] for m in up.messages)
    assert ddl("SELECT published_at IS NOT NULL FROM outbox WHERE id = :i", i=row)[0][0]
