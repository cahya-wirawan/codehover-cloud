"""Telemetry carries no tenant data (T18; 12, A24) and the operational gauges are aggregates only.

A replay build, a search, an export download and a failed sign-in run with every log record captured as
emitted JSON; then the logs and the whole metrics exposition are searched for source text, secrets from
the fixture repository, the search query, file paths, cookies, CSRF tokens and tenant IDs.
"""

from __future__ import annotations

import io
import logging
import re
import secrets
from collections.abc import Iterator

import httpx
import pytest
import sqlalchemy as sa
from prometheus_client import generate_latest
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import metrics
from app.core.db import session_factory
from app.core.logging import JsonFormatter, configure_logging
from tests.integration.auth_support import FakeIdP, ddl, unsafe
from tests.integration.conftest import RUNTIME_URL, SYSTEM_URL, World, engine, needs_db, needs_system
from tests.integration.test_builds import drive
from tests.integration.test_generation import runtime_for
from tests.integration.test_github import FakeGitHub, github  # noqa: F401
from tests.integration.test_publication import ready_build

pytestmark = [pytest.mark.integration, needs_db]
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
FIXTURE_SECRETS = ("hunter2", "AKIAABCDEFGHIJKLMNOP", "SECRET_TOKEN", "not really a key")


@pytest.fixture
def emitted() -> Iterator[io.StringIO]:
    """Every record at DEBUG and above, with the production logging configuration (configure_logging)."""
    root = logging.getLogger()
    saved = (root.level, list(root.handlers))
    levels = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    configure_logging("DEBUG")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    try:
        yield stream
    finally:
        root.setLevel(saved[0])
        root.handlers[:] = saved[1]
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)


async def test_logs_and_metrics_carry_no_source_secrets_queries_paths_or_ids(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, emitted: io.StringIO
) -> None:
    owner, ws, project, build, app = await ready_build(client, idp, github)
    query = f"needle{secrets.token_hex(4)}"
    assert (await owner.get(f"/api/v1/builds/{build}/search", params={"q": query})).status_code == 200
    export = await unsafe(
        owner,
        "POST",
        f"/api/v1/builds/{build}/exports",
        json={"format": "kb_json"},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    await drive(app, build, runtime=runtime_for(app))
    assert (await owner.get(f"/api/v1/exports/{export.json()['id']}/download")).status_code == 200
    forged = await client.get(
        "/api/v1/auth/google/callback", params={"code": "code-" + secrets.token_hex(8), "state": "x" * 43}
    )
    assert forged.status_code == 302 and "error=" in forged.headers["location"]

    logs = emitted.getvalue()
    exposition = generate_latest(metrics.REGISTRY).decode()
    session_cookie = next(iter(owner.cookies.values()))
    csrf = (await owner.get("/api/v1/auth/csrf")).json()["csrf_token"]
    files = [f["path"] for f in (await owner.get(f"/api/v1/builds/{build}/tree")).json()["items"]]
    forbidden = [*FIXTURE_SECRETS, query, session_cookie, csrf, "def main", "Entry point."]
    for text in (logs, exposition):
        leaked = [f for f in forbidden if f and f in text]
        # Paths as whole names: a short directory like `web` is not leaked by the route `/webhooks/...`.
        leaked += [f for f in files if re.search(rf"(?<![\w.-]){re.escape(f)}(?![\w.-])", text)]
        assert not leaked, leaked
    assert not UUID_RE.search(exposition)  # no tenant, build, user or request IDs as label values
    for entity in (ws, project, build):
        assert entity not in exposition

    # The series exist with bounded labels: route templates, stages, providers.
    assert 'route="/builds/{build_id}/search"' in exposition  # templates, relative to /api/v1
    assert re.search(
        r'codehover_task_outcomes_total\{outcome="completed",stage="explain"\} [1-9]', exposition
    )
    assert re.search(
        r'codehover_provider_calls_total\{outcome="succeeded",provider="replay",purpose="explain"\}',
        exposition,
    )
    assert re.search(r'codehover_auth_failures_total\{reason="[a-z_]+"\} [1-9]', exposition)
    for value in re.findall(r'route="([^"]*)"', exposition):
        assert value == "unmatched" or (value.startswith("/") and UUID_RE.search(value) is None), value


def test_metric_labels_are_bounded() -> None:
    assert metrics.label("explain") == "explain"
    for hostile in (
        "src/settings.py",
        "a" * 65,
        "Robert'); DROP",
        "",
        None,
        "01a1ef2b-034e-73fc-83a7-b8574a352db3x" * 2,
    ):
        assert metrics.label(hostile) == "other"


@needs_system
async def test_operational_gauges_are_aggregates_for_the_system_role_only(world: World) -> None:
    assert SYSTEM_URL
    async with session_factory(create_async_engine(SYSTEM_URL))() as db:
        values = await metrics.collect_queue_gauges(db)
    assert set(values) == {
        "runnable_tasks",
        "oldest_runnable_seconds",
        "leased_tasks",
        "expired_leases",
        "outbox_unpublished",
        "outbox_oldest_seconds",
        "deletions_pending",
        "deletion_oldest_seconds",
        "unknown_attempts",
        "unknown_oldest_seconds",
        "usage_drift_micro_usd",
    }
    assert all(isinstance(v, float) and v >= 0 for v in values.values())
    # Drift is measured exactly: knock one account's counter off its ledger by a known amount.
    ddl(
        "UPDATE usage_accounts SET reserved_micro_usd = reserved_micro_usd + 1234 WHERE id = :a",
        a=world.a.account,
    )
    try:
        async with session_factory(create_async_engine(SYSTEM_URL))() as db:
            drifted = await metrics.collect_queue_gauges(db)
        assert drifted["usage_drift_micro_usd"] - values["usage_drift_micro_usd"] == 1234
    finally:
        ddl(
            "UPDATE usage_accounts SET reserved_micro_usd = reserved_micro_usd - 1234 WHERE id = :a",
            a=world.a.account,
        )
    with engine(RUNTIME_URL).connect() as conn, pytest.raises(sa.exc.ProgrammingError):
        conn.execute(sa.text("SELECT * FROM app_private.ops_gauges()"))  # not a runtime privilege
