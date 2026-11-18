"""Build progress over server-sent events (T13; 06 "Server-sent events", 10, A17)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app.modules.projects import events
from tests.integration.auth_support import FakeIdP, ddl
from tests.integration.conftest import needs_db
from tests.integration.test_builds import drive, setup_project
from tests.integration.test_generation import generation_build, runtime_for
from tests.integration.test_github import FakeGitHub, github  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]
SSE = {"Accept": "text/event-stream"}


def parse(body: str) -> list[dict[str, Any]]:
    frames = []
    for block in body.split("\n\n"):
        fields: dict[str, Any] = {}
        for line in block.split("\n"):
            if line.startswith(":"):
                fields["comment"] = line[1:].strip()
            elif ": " in line:
                key, value = line.split(": ", 1)
                fields[key] = value
        if fields:
            frames.append(fields)
    return frames


def fast(app: Any) -> None:
    app.state.event_timing = events.Timing(poll_seconds=0.05, heartbeat_seconds=0.1, recheck_seconds=0.1)


async def test_stream_replays_history_and_resumes_from_last_event_id(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, build, app = await generation_build(client, idp, github, preset="replay-v2")
    fast(app)
    await drive(app, build, runtime=runtime_for(app))
    response = await owner.get(f"/api/v1/builds/{build}/events", headers=SSE)
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    frames = [f for f in parse(response.text) if "event" in f]
    sequences = [int(f["id"]) for f in frames]
    assert sequences == sorted(sequences) and sequences[0] == 1
    assert frames[0]["event"] == "build.queued" and frames[-1]["event"] == "build.ready"
    resumed = await owner.get(
        f"/api/v1/builds/{build}/events", headers={**SSE, "Last-Event-ID": str(sequences[-3])}
    )
    assert [int(f["id"]) for f in parse(resumed.text) if "event" in f] == sequences[-2:]
    polling = (await owner.get(f"/api/v1/builds/{build}/events")).json()  # JSON fallback still works
    assert [e["sequence"] for e in polling["items"]] == sequences


async def test_stream_follows_a_running_build_until_it_is_ready(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, build, app = await generation_build(client, idp, github, preset="replay-v2")
    fast(app)

    async def run_build() -> None:
        await asyncio.sleep(0.2)
        await drive(app, build, runtime=runtime_for(app))

    response, _ = await asyncio.gather(owner.get(f"/api/v1/builds/{build}/events", headers=SSE), run_build())
    names = [f["event"] for f in parse(response.text) if "event" in f]
    assert names[0] == "build.queued" and names[-1] == "build.ready"
    assert names.count("stage.completed") == 8


async def test_a_revoked_session_ends_the_stream(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """A17: access is re-checked while streaming; the build never finishes here."""
    owner, _ws, build, app = await generation_build(client, idp, github, preset="replay-v2")
    fast(app)

    async def revoke() -> None:
        await asyncio.sleep(0.4)
        ddl(
            "UPDATE sessions SET revoked_at = now() WHERE revoked_at IS NULL AND user_id = (SELECT created_by FROM builds WHERE id = :b)",
            b=build,
        )

    response, _ = await asyncio.wait_for(
        asyncio.gather(owner.get(f"/api/v1/builds/{build}/events", headers=SSE), revoke()), timeout=10
    )
    frames = parse(response.text)
    assert any(f.get("comment") == "heartbeat" for f in frames)  # an idle stream keeps sending heartbeats
    assert frames[-1]["event"] == "access.revoked"
    assert (await owner.get(f"/api/v1/builds/{build}")).status_code == 401


async def test_strangers_cannot_stream(client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub) -> None:
    _owner, _ws, build, _app = await generation_build(client, idp, github, preset="replay-v2")
    stranger, _, _ = await setup_project(client, idp, github)
    assert (await stranger.get(f"/api/v1/builds/{build}/events", headers=SSE)).status_code == 404


def test_stream_limits_per_session_and_workspace() -> None:
    from uuid import uuid4

    from app.core.errors import ApiError

    session, workspace = uuid4(), uuid4()
    slots = [events.StreamSlot(session, workspace).__enter__() for _ in range(events.PER_SESSION)]
    with pytest.raises(ApiError) as refused:
        events.StreamSlot(session, workspace).__enter__()
    assert refused.value.code == "STREAM_LIMIT" and refused.value.status_code == 429
    slots.pop().__exit__()
    events.StreamSlot(session, workspace).__enter__().__exit__()  # a released slot can be taken again
    others = [
        events.StreamSlot(uuid4(), workspace).__enter__() for _ in range(events.PER_WORKSPACE - len(slots))
    ]
    with pytest.raises(ApiError):
        events.StreamSlot(uuid4(), workspace).__enter__()
    for slot in [*slots, *others]:
        slot.__exit__()
