"""Retention sweeps (T17; 05 "Retention and migrations", 08 "Lifecycle and controls").

Ages are simulated by moving timestamps back; every window and its boundary is checked.
"""

from __future__ import annotations

import secrets
from typing import Any
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.db import session_factory
from app.engine.keys import build_prefix
from app.jobs import control, retention
from tests.integration.auth_support import FakeIdP, ddl, unsafe
from tests.integration.conftest import SYSTEM_URL, needs_db, needs_system
from tests.integration.test_builds import drive
from tests.integration.test_generation import runtime_for
from tests.integration.test_github import FakeGitHub, github  # noqa: F401
from tests.integration.test_publication import publish, ready_build, second_build

pytestmark = [pytest.mark.integration, needs_db]


def age(table: str, column: str, days: int, **where: Any) -> None:
    ((key, value),) = where.items()
    sql = f"UPDATE {table} SET {column} = now() - make_interval(days => :d) WHERE {key} = :v"  # noqa: S608
    ddl(sql, d=days, v=value)


def rows_of(table: str, build: str) -> int:
    sql = f"SELECT count(*) FROM {table} WHERE build_id = :b"  # noqa: S608 (fixed table names)
    count: int = ddl(sql, b=build)[0][0]
    return count


def build_objects(app: Any, ws: str, project: str, build: str) -> list[str]:
    prefix = build_prefix(UUID(ws), UUID(project), UUID(build))
    return [k for k in app.state.objects.objects if k.startswith(prefix)]


async def test_exports_expire_and_lose_their_object(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, _project, build, app = await ready_build(client, idp, github)
    created = await unsafe(
        owner,
        "POST",
        f"/api/v1/builds/{build}/exports",
        json={"format": "kb_json"},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    await drive(app, build, runtime=runtime_for(app))
    export_id = created.json()["id"]
    key = ddl("SELECT object_key FROM exports WHERE id = :e", e=export_id)[0][0]
    rt = runtime_for(app)
    await retention.sweep_workspace(rt.sessions, app.state.objects, UUID(ws))
    assert key in app.state.objects.objects  # within its 24 hours: kept

    ddl("UPDATE exports SET expires_at = now() - interval '1 minute' WHERE id = :e", e=export_id)
    counts = await retention.sweep_workspace(rt.sessions, app.state.objects, UUID(ws))
    assert counts["exports"] == 1 and key not in app.state.objects.objects
    assert ddl("SELECT status, object_key FROM exports WHERE id = :e", e=export_id)[0] == ("expired", None)
    gone = await owner.get(f"/api/v1/exports/{export_id}/download")
    assert gone.status_code == 410 and gone.json()["error"]["code"] == "EXPORT_EXPIRED"
    assert (await owner.get(f"/api/v1/exports/{export_id}")).json()["expired"] is True


async def test_old_builds_lose_content_but_the_published_and_newest_stay(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, ws, project, first, app = await ready_build(client, idp, github)
    second = await second_build(owner, app, project)
    third = await second_build(owner, app, project)
    monkeypatch.setattr(retention, "KEEP_READY_BUILDS", 1)
    for build in (first, second, third):
        age("builds", "completed_at", 31, id=build)
    published = await publish(owner, project, build_id=first)  # the published build is never swept
    assert published.status_code == 200, published.text
    assert build_objects(app, ws, project, second)

    rt = runtime_for(app)
    counts = await retention.sweep_workspace(rt.sessions, app.state.objects, UUID(ws))
    assert counts["builds"] == 1
    purged = ddl("SELECT id FROM builds WHERE project_id = :p AND content_purged_at IS NOT NULL", p=project)
    assert [str(r[0]) for r in purged] == [second]  # not the published one, not the newest
    for table in ("symbols", "file_explanations", "guide_pages", "search_documents", "artifacts"):
        assert rows_of(table, second) == 0, table
        assert rows_of(table, third) > 0 or table == "artifacts", table
    assert build_objects(app, ws, project, second) == [] and build_objects(app, ws, project, third)
    assert (await owner.get(f"/api/v1/builds/{second}")).json()["content_purged_at"]

    etag = (await owner.get(f"/api/v1/projects/{project}/publication")).headers["etag"]
    stale = await publish(owner, project, etag, build_id=second)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "BUILD_EXPIRED"
    export = await unsafe(
        owner,
        "POST",
        f"/api/v1/builds/{second}/exports",
        json={"format": "kb_json"},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    assert export.status_code == 409 and export.json()["error"]["code"] == "BUILD_EXPIRED"
    again = await retention.sweep_workspace(rt.sessions, app.state.objects, UUID(ws))
    assert again["builds"] == 0  # idempotent


async def test_failed_builds_events_cache_and_estimates_age_out(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, project, first, app = await ready_build(client, idp, github)
    failed = await second_build(owner, app, project)
    ddl("UPDATE builds SET status = 'failed' WHERE id = :b", b=failed)
    age("builds", "completed_at", 6, id=failed)
    rt = runtime_for(app)
    assert (await retention.sweep_workspace(rt.sessions, app.state.objects, UUID(ws)))[
        "builds"
    ] == 0  # 6 days
    age("builds", "completed_at", 8, id=failed)
    assert (await retention.sweep_workspace(rt.sessions, app.state.objects, UUID(ws)))["builds"] == 1

    events = ddl("SELECT count(*) FROM build_events WHERE build_id = :b", b=first)[0][0]
    assert events > 0
    age("build_events", "created_at", 31, build_id=first)
    ddl("UPDATE generation_cache SET expires_at = now() - interval '1 second' WHERE workspace_id = :w", w=ws)
    snapshot = ddl("SELECT snapshot_id FROM builds WHERE id = :b", b=first)[0][0]
    unused = ddl(
        "INSERT INTO estimates(workspace_id, project_id, snapshot_id, config_digest, price_version, low_micro_usd,"
        " high_micro_usd, token_estimate, expires_at, created_by, config)"
        " SELECT workspace_id, project_id, snapshot_id, config_digest, price_version, low_micro_usd, high_micro_usd,"
        " token_estimate, expires_at, created_by, config FROM estimates WHERE snapshot_id = :s LIMIT 1 RETURNING id",
        s=snapshot,
    )[0][0]
    ddl("UPDATE estimates SET created_at = now() - interval '31 days' WHERE snapshot_id = :s", s=snapshot)
    counts = await retention.sweep_workspace(rt.sessions, app.state.objects, UUID(ws))
    assert counts["events"] == events and counts["cache"] >= 0
    assert ddl("SELECT count(*) FROM build_events WHERE build_id = :b", b=first)[0][0] == 0
    assert ddl("SELECT count(*) FROM generation_cache WHERE workspace_id = :w", w=ws)[0][0] == 0
    assert ddl("SELECT count(*) FROM estimates WHERE id = :e", e=unused)[0][0] == 0
    used = ddl(
        "SELECT count(*) FROM estimates e JOIN builds b ON b.estimate_id = e.id WHERE b.id = :b", b=first
    )
    assert used[0][0] == 1  # an estimate a build refers to stays


async def test_identity_and_audit_rows_age_out_only_past_their_window(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, _project, _build, app = await ready_build(client, idp, github)
    me = (await owner.get("/api/v1/me")).json()["id"]
    old_audit = ddl(
        "INSERT INTO audit_events(workspace_id, actor_user_id, action, resource_type, metadata, created_at)"
        " VALUES (:w, :u, 'project.created', 'project', '{}', now() - interval '181 days') RETURNING id",
        w=ws,
        u=me,
    )[0][0]
    young_audit = ddl(
        "INSERT INTO audit_events(workspace_id, actor_user_id, action, resource_type, metadata, created_at)"
        " VALUES (:w, :u, 'project.created', 'project', '{}', now() - interval '179 days') RETURNING id",
        w=ws,
        u=me,
    )[0][0]
    ended = ddl(
        "INSERT INTO sessions(user_id, token_hash, csrf_hash, created_at, expires_at, last_seen_at,"
        " reauthenticated_at, revoked_at) VALUES (:u, :t, :c, now() - interval '40 days',"
        " now() - interval '10 days', now() - interval '35 days', now() - interval '40 days',"
        " now() - interval '31 days') RETURNING id",
        u=me,
        t=secrets.token_bytes(32),
        c=secrets.token_bytes(32),
    )[0][0]
    idempotency = ddl("SELECT count(*) FROM idempotency_keys WHERE user_id = :u", u=me)[0][0]
    assert idempotency > 0
    ddl("UPDATE idempotency_keys SET expires_at = now() - interval '1 second' WHERE user_id = :u", u=me)

    rt = runtime_for(app)
    totals = await retention.sweep(rt.sessions, app.state.objects)
    assert (
        totals["audit_events"] >= 1 and totals["sessions"] >= 1 and totals["idempotency_keys"] >= idempotency
    )
    assert ddl("SELECT count(*) FROM audit_events WHERE id = :a", a=old_audit)[0][0] == 0
    assert ddl("SELECT count(*) FROM audit_events WHERE id = :a", a=young_audit)[0][0] == 1  # 179 days: kept
    assert ddl("SELECT count(*) FROM sessions WHERE id = :s", s=ended)[0][0] == 0
    assert (await owner.get("/api/v1/me")).status_code == 200  # the live session is untouched
    assert ddl("SELECT count(*) FROM idempotency_keys WHERE user_id = :u", u=me)[0][0] == 0


@needs_system
async def test_dispatched_outbox_rows_are_kept_seven_days() -> None:
    old, recent = (
        ddl(
            "INSERT INTO outbox(aggregate_id, event_type, payload, published_at)"
            " VALUES (:a, 'task.ready', '{}', now() - make_interval(days => :d)) RETURNING id",
            a=str(UUID(int=n)),
            d=days,
        )[0][0]
        for n, days in ((1, 8), (2, 6))
    )
    assert SYSTEM_URL
    async with session_factory(create_async_engine(SYSTEM_URL))() as db:
        assert await control.sweep_outbox(db) >= 1
    assert ddl("SELECT count(*) FROM outbox WHERE id = :i", i=old)[0][0] == 0
    assert ddl("SELECT count(*) FROM outbox WHERE id = :i", i=recent)[0][0] == 1
    ddl("DELETE FROM outbox WHERE id = :i", i=recent)


async def test_a_publish_committed_while_the_sweep_waits_keeps_its_build(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security review of 0.5.15: the sweep must re-check the pointer after its lock, with a fresh snapshot."""
    import asyncio

    from tests.integration.conftest import MIGRATION_URL, engine

    owner, ws, project, first, app = await ready_build(client, idp, github)
    newest = await second_build(owner, app, project)
    assert (await publish(owner, project, build_id=newest)).status_code == 200
    age("builds", "completed_at", 31, id=first)
    monkeypatch.setattr(retention, "KEEP_READY_BUILDS", 1)
    rt = runtime_for(app)
    candidate = ddl("SELECT id, project_id FROM builds WHERE id = :b", b=first)[0]

    with engine(MIGRATION_URL).connect() as publisher:
        tx = publisher.begin()  # a publish in flight: FOR SHARE on the build, pointer moved, not committed
        publisher.execute(sa.text("SELECT id FROM builds WHERE id = :b FOR SHARE"), {"b": first})
        publisher.execute(
            sa.text("UPDATE publications SET build_id = :b, version = version + 1 WHERE project_id = :p"),
            {"b": first, "p": project},
        )
        sweep = asyncio.create_task(
            retention._purge_build(rt.sessions, app.state.objects, UUID(ws), candidate)
        )
        for _ in range(100):  # until the sweep's backend really waits on the publish's row lock
            await asyncio.sleep(0.05)
            waiting = ddl(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                " AND query LIKE '%FROM builds WHERE workspace_id%FOR UPDATE%'"
            )[0][0]
            if waiting:
                break
        assert waiting and not sweep.done()
        tx.commit()
        assert await sweep is False
    assert ddl("SELECT content_purged_at FROM builds WHERE id = :b", b=first)[0][0] is None
    assert rows_of("symbols", first) > 0
