"""End-to-end replay build (M1 exit criterion) and job crash/retry semantics on PostgreSQL 18 (T06-T09).

The build goes through the real API, the real stage handlers and the real runner; only GitHub is faked
(commit, tarball via codeload redirect) and object storage is in memory. On Linux the index stage runs
inside the seccomp sandbox.
"""

from __future__ import annotations

import io
import secrets
import tarfile
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.db import session_factory
from app.jobs import control, runner, store
from app.jobs.envelope import TaskEnvelope
from app.jobs.stages import Runtime
from tests.integration.auth_support import FakeIdP, ddl, unsafe
from tests.integration.conftest import RUNTIME_URL, SYSTEM_URL, needs_db, needs_system
from tests.integration.test_github import FakeGitHub, connect, github, github_user, workspace  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]

FIXTURE = {
    "src/app.py": '"""Tiny app."""\nfrom src.util import helper\n\n\ndef main(argv):\n    """Entry point. Runs the helper."""\n    return helper(argv)\n\n\nclass Greeter:\n    def greet(self, name):\n        return f"hi {name}"\n',
    "src/util.py": "def helper(args):\n    return len(args)\n",
    "web/index.ts": 'export function render(x: number): string { return String(x) }\nconst label = "é"; function badge(): string { return render(1) }\n',
    "README.md": "# Fixture\n",
    ".env": "SECRET_TOKEN=hunter2\n",
    "deploy/key.pem": "not really a key\n",
    "src/settings.py": "AWS_KEY = 'AKIAABCDEFGHIJKLMNOP'\n",
}


def fixture_tarball() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, content in FIXTURE.items():
            data = content.encode()
            info = tarfile.TarInfo(f"octo-api-c0ffee/{path}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


async def drive(app: Any, build_id: str, rounds: int = 16, runtime: Runtime | None = None) -> list[str]:
    """Deliver pending notifications to the runner until the build stops, like the consumer would."""
    assert RUNTIME_URL
    rt = runtime or Runtime(
        sessions=session_factory(create_async_engine(RUNTIME_URL)),
        store=app.state.objects,
        github=app.state.github_app,
        allow_unsandboxed=True,
        catalog=app.state.catalog,
    )
    outcomes: list[str] = []
    for _ in range(rounds):
        rows = ddl(
            "UPDATE outbox SET published_at = now() WHERE published_at IS NULL"
            " AND payload->>'build_id' = :b RETURNING payload",
            b=build_id,
        )
        if not rows:
            break
        for (payload,) in rows:
            outcomes.append(await runner.process(rt, TaskEnvelope.model_validate(payload)))
    return outcomes


async def setup_project(
    client: httpx.AsyncClient, idp: FakeIdP, gh: FakeGitHub
) -> tuple[httpx.AsyncClient, str, str]:
    owner, gh_user = await github_user(client, idp)
    ws = await workspace(owner)
    installation = gh.add_installation(gh_user)
    for repo in gh.repos[installation]:
        gh.tarballs[repo["id"]] = fixture_tarball()
    await connect(owner, gh, ws, installation, gh_user)
    connection = (await owner.get(f"/api/v1/workspaces/{ws}/connections")).json()["items"][0]["id"]
    repo = (
        await owner.get(f"/api/v1/workspaces/{ws}/repositories", params={"connection_id": connection})
    ).json()["items"][0]
    created = await unsafe(
        owner,
        "POST",
        f"/api/v1/workspaces/{ws}/projects",
        json={"repository_id": repo["id"], "name": "API", "slug": "api"},
    )
    assert created.status_code == 201, created.text
    return owner, ws, created.json()["id"]


async def start(owner: httpx.AsyncClient, project: str, key: str | None = None) -> httpx.Response:
    return await unsafe(
        owner,
        "POST",
        f"/api/v1/projects/{project}/builds",
        json={},
        headers={"Idempotency-Key": key or secrets.token_hex(12)},
    )


# ---------------------------------------------------------------- end to end
async def test_replay_build_end_to_end_and_private_viewer(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, project = await setup_project(client, idp, github)
    started = await start(owner, project)
    assert started.status_code == 202 and started.json()["status"] == "queued", started.text
    build = started.json()["id"]
    outcomes = await drive(client.app, build)  # type: ignore[attr-defined]
    assert outcomes == ["completed"] * 4, outcomes
    status = (await owner.get(f"/api/v1/builds/{build}")).json()
    assert status["status"] == "ready" and status["commit_sha"] == github.commit_sha
    assert [(s["stage"], s["state"]) for s in status["stages"]] == [
        ("acquire", "succeeded"),
        ("index", "succeeded"),
        ("explain", "succeeded"),
        ("render", "succeeded"),
    ]

    files = {
        r[0]: (r[1], r[2])
        for r in ddl(
            "SELECT f.path, f.scan_status, f.scan_reason FROM source_files f JOIN builds b"
            " ON b.workspace_id = f.workspace_id AND b.snapshot_id = f.snapshot_id WHERE b.id = :b",
            b=build,
        )
    }
    assert files[".env"] == ("excluded", "environment_file")
    assert files["deploy/key.pem"] == ("excluded", "credential_file")
    assert files["src/settings.py"] == ("blocked", "secret_detected")
    assert files["README.md"][0] == "unsupported" and files["src/app.py"][0] == "selected"
    objects = client.app.state.objects.objects  # type: ignore[attr-defined]
    assert not any(b"hunter2" in data or b"AKIAABCDEFGHIJKLMNOP" in data for data, _ in objects.values())

    root = (await owner.get(f"/api/v1/builds/{build}/tree")).json()["items"]
    assert [(e["name"], e["type"]) for e in root][:3] == [("deploy", "dir"), ("src", "dir"), ("web", "dir")]
    src = {
        e["name"]: e
        for e in (await owner.get(f"/api/v1/builds/{build}/tree", params={"path": "src"})).json()["items"]
    }
    view = (await owner.get(f"/api/v1/builds/{build}/files/{src['app.py']['file_id']}")).json()
    assert view["lines"][4] == "def main(argv):"  # line 5: docstring, import, two blank lines first
    symbols = {s["symbol_key"]: s for s in view["symbols"]}
    assert set(symbols) == {"main", "Greeter", "Greeter.greet"}
    assert symbols["Greeter.greet"]["kind"] == "method"
    assert symbols["main"]["summary"] == "Entry point."
    card = (await owner.get(f"/api/v1/builds/{build}/symbols/{symbols['main']['id']}")).json()
    assert [u["name"] for u in card["uses"]] == ["helper"] and card["uses"][0]["resolution"] == "name"
    assert card["card"]["provenance"]["generator"] == "replay-v1"
    blocked = await owner.get(f"/api/v1/builds/{build}/files/{src['settings.py']['file_id']}")
    assert blocked.status_code == 409 and blocked.json()["error"]["details"]["reason"] == "secret_detected"

    web = {
        e["name"]: e
        for e in (await owner.get(f"/api/v1/builds/{build}/tree", params={"path": "web"})).json()["items"]
    }
    ts_view = (await owner.get(f"/api/v1/builds/{build}/files/{web['index.ts']['file_id']}")).json()
    badge = next(s for s in ts_view["symbols"] if s["name"] == "badge")
    line = ts_view["lines"][badge["start_line"] - 1]
    assert line.index("function badge") == badge["start_column"]  # Unicode scalar column, not UTF-8 bytes
    manifest = ddl("SELECT object_key FROM artifacts WHERE build_id = :b AND kind = 'manifest'", b=build)
    assert manifest and manifest[0][0].startswith(f"workspaces/{ws}/projects/{project}/builds/{build}/")
    await owner.aclose()


async def test_build_start_is_idempotent_and_single_active(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project = await setup_project(client, idp, github)
    key = secrets.token_hex(12)
    first, replay = await start(owner, project, key), await start(owner, project, key)
    assert first.status_code == replay.status_code == 202 and first.json()["id"] == replay.json()["id"]
    different = await unsafe(
        owner,
        "POST",
        f"/api/v1/projects/{project}/builds",
        json={"ref": "v2"},
        headers={"Idempotency-Key": key},
    )
    assert different.status_code == 409 and different.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    second = await start(owner, project)
    assert second.status_code == 409 and second.json()["error"]["code"] == "BUILD_ACTIVE"
    missing = await unsafe(owner, "POST", f"/api/v1/projects/{project}/builds", json={})
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    await owner.aclose()


async def test_second_build_reuses_the_immutable_snapshot(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project = await setup_project(client, idp, github)
    first = (await start(owner, project)).json()["id"]
    await drive(client.app, first)  # type: ignore[attr-defined]
    second = (await start(owner, project)).json()["id"]
    assert await drive(client.app, second) == ["completed"] * 3  # type: ignore[attr-defined]
    stages = (await owner.get(f"/api/v1/builds/{second}")).json()["stages"]
    assert stages[0] == {"stage": "acquire", "state": "skipped", "error_code": None}
    await owner.aclose()


async def test_hostile_archive_rejects_snapshot_and_fails_build(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project = await setup_project(client, idp, github)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("octo-api-c0ffee/../../etc/passwd")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    for repo_id in github.tarballs:
        github.tarballs[repo_id] = buffer.getvalue()
    build = (await start(owner, project)).json()["id"]
    assert await drive(client.app, build) == ["failed"]  # type: ignore[attr-defined]
    status = (await owner.get(f"/api/v1/builds/{build}")).json()
    assert (status["status"], status["snapshot_status"], status["rejection_code"]) == (
        "failed",
        "rejected",
        "path_traversal",
    )
    assert status["stages"][0]["error_code"] == "archive_path_traversal"
    await owner.aclose()


async def test_viewer_is_private_to_the_workspace(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project = await setup_project(client, idp, github)
    build = (await start(owner, project)).json()["id"]
    await drive(client.app, build)  # type: ignore[attr-defined]
    stranger, _ = await github_user(client, idp)
    for url in (f"/api/v1/projects/{project}", f"/api/v1/builds/{build}", f"/api/v1/builds/{build}/tree"):
        assert (await stranger.get(url)).status_code == 404
    assert (
        await stranger.get(f"/api/v1/builds/{secrets.token_hex(16)[:8]}-0000-7000-8000-000000000000")
    ).status_code in (404, 422)
    await stranger.aclose()
    await owner.aclose()


# ---------------------------------------------------------------- job semantics (04 "Outbox, delivery and leases")
async def queued_task(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> tuple[Runtime, TaskEnvelope]:
    owner, _ws, project = await setup_project(client, idp, github)
    build = (await start(owner, project)).json()["id"]
    await owner.aclose()
    (payload,) = ddl("SELECT payload FROM outbox WHERE payload->>'build_id' = :b", b=build)[0]
    assert RUNTIME_URL
    rt = Runtime(
        sessions=session_factory(create_async_engine(RUNTIME_URL)),
        store=client.app.state.objects,  # type: ignore[attr-defined]
        github=client.app.state.github_app,
        allow_unsandboxed=True,
    )  # type: ignore[attr-defined]
    return rt, TaskEnvelope.model_validate(payload)


async def test_duplicate_notifications_claim_once(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    rt, envelope = await queued_task(client, idp, github)
    async with rt.sessions() as db:
        first = await store.claim(db, envelope)
    async with rt.sessions() as db:
        second = await store.claim(db, envelope)
    assert first is not None and second is None


@needs_system
async def test_lost_lease_is_fenced_and_reconciled(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    rt, envelope = await queued_task(client, idp, github)
    async with rt.sessions() as db:
        stale = await store.claim(db, envelope)  # worker A claims, then "crashes"
    assert stale is not None
    ddl("UPDATE build_tasks SET lease_until = now() - interval '1 second' WHERE id = :t", t=envelope.task_id)
    assert SYSTEM_URL
    async with session_factory(create_async_engine(SYSTEM_URL))() as sys_db:
        counts = await control.reconcile_once(sys_db)
    assert counts["expired"] >= 1 and counts["notified"] >= 1
    assert await runner.process(rt, envelope) == "completed"  # worker B finishes the stage
    async with rt.sessions() as db:
        assert await store.heartbeat(db, stale) is False
    async with rt.sessions() as db:
        with pytest.raises(store.LeaseLost):
            await store.complete(db, stale)  # worker A wakes up: its result is discarded
    states = ddl(
        "SELECT stage, state, lease_generation FROM build_tasks WHERE build_id = :b ORDER BY created_at",
        b=envelope.build_id,
    )
    assert (
        states[0][1] == "succeeded" and states[0][2] == 2 and [s[0] for s in states] == ["acquire", "index"]
    )


async def test_transient_failures_retry_then_fail_the_build(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    rt, envelope = await queued_task(client, idp, github)
    seen = []
    for attempt in range(1, store.MAX_ATTEMPTS + 1):
        ddl("UPDATE build_tasks SET next_attempt_at = now() WHERE id = :t", t=envelope.task_id)
        async with rt.sessions() as db:
            claimed = await store.claim(db, envelope)
        assert claimed is not None and claimed.attempt == attempt
        async with rt.sessions() as db:
            seen.append(await store.fail(db, claimed, "github_network", transient=True))
    assert seen == ["retry_wait"] * (store.MAX_ATTEMPTS - 1) + ["failed"]
    assert ddl("SELECT status FROM builds WHERE id = :b", b=envelope.build_id)[0][0] == "failed"
    retry_delay = ddl(
        "SELECT count(*) FROM build_events WHERE build_id = :b AND event_type = 'task.retry_scheduled'",
        b=envelope.build_id,
    )[0][0]
    assert retry_delay == store.MAX_ATTEMPTS - 1


async def test_cancelled_build_tasks_are_not_claimed(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    rt, envelope = await queued_task(client, idp, github)
    ddl("UPDATE builds SET cancel_requested_at = now() WHERE id = :b", b=envelope.build_id)
    assert await runner.process(rt, envelope) == "not_runnable"


@needs_system
async def test_lost_message_is_renotified(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    _rt, envelope = await queued_task(client, idp, github)
    ddl(
        "UPDATE outbox SET published_at = now() WHERE aggregate_id = :t", t=envelope.task_id
    )  # broker lost it
    ddl("UPDATE build_tasks SET notified_at = now() - interval '2 minutes' WHERE id = :t", t=envelope.task_id)
    assert SYSTEM_URL
    async with session_factory(create_async_engine(SYSTEM_URL))() as sys_db:
        await control.reconcile_once(sys_db)
    pending = ddl(
        "SELECT count(*) FROM outbox WHERE aggregate_id = :t AND published_at IS NULL", t=envelope.task_id
    )
    assert pending[0][0] == 1


def tarball_of(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(f"octo-api-c0ffee/{path}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


async def test_names_repeated_across_files_index_as_separate_symbols(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """0021: keys are unique per file; `main` in two files must not fail the index stage."""
    owner, _ws, project = await setup_project(client, idp, github)
    repeated = {
        "a/cli.py": 'def main():\n    """Run a."""\n    return 1\n\n\nclass Tool:\n    def run(self):\n        return 1\n',
        "b/cli.py": 'def main():\n    """Run b."""\n    return 2\n\n\nclass Tool:\n    def run(self):\n        return 2\n',
    }
    for repo_id in list(github.tarballs):
        github.tarballs[repo_id] = tarball_of(repeated)
    build = (await start(owner, project)).json()["id"]
    await drive(client.app, build)  # type: ignore[attr-defined]
    assert (await owner.get(f"/api/v1/builds/{build}")).json()["status"] == "ready"
    rows = ddl("SELECT symbol_key, count(*) FROM symbols WHERE build_id = :b GROUP BY 1 ORDER BY 1", b=build)
    assert sorted((k, n) for k, n in rows) == [("Tool", 2), ("Tool.run", 2), ("main", 2)]


async def test_a_failing_apply_retries_instead_of_holding_the_lease(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stage whose writes fail is a transient failure (retried in SQL), never a task left leased."""
    owner, _ws, project = await setup_project(client, idp, github)
    build = (await start(owner, project)).json()["id"]
    from app.jobs import stages

    original = stages.HANDLERS["acquire"]

    async def breaking(rt: Any, claimed: Any) -> Any:
        await original(rt, claimed)

        async def apply(_db: Any) -> None:
            raise RuntimeError("write failed")

        return apply

    monkeypatch.setitem(stages.HANDLERS, "acquire", breaking)
    outcomes = await drive(client.app, build, rounds=1)  # type: ignore[attr-defined]
    assert outcomes == ["retry_wait"]
    state = ddl("SELECT state, error_code, lease_until FROM build_tasks WHERE build_id = :b", b=build)[0]
    assert state == ("retry_wait", "internal_RuntimeError", None)
