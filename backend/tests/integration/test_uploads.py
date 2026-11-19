"""ZIP repository import (M6; 01 P1, 08 "Archive traversal"): an uploaded project built end to end, the
archive limits applied in the worker, and GitHub-only paths refused for uploads."""

from __future__ import annotations

import io
import secrets
import zipfile
from typing import Any

import httpx
import pytest

from app.engine.keys import upload_key
from app.modules.projects import router as projects_router
from app.modules.projects import uploads
from tests.integration.auth_support import FakeIdP, ddl, unsafe
from tests.integration.conftest import needs_db
from tests.integration.test_budgets import drive_snapshot, make_estimate, start_from
from tests.integration.test_builds import drive, setup_project
from tests.integration.test_generation import runtime_for
from tests.integration.test_github import FakeGitHub, github, workspace  # noqa: F401
from tests.integration.test_workspaces import NewUser, code, join, new_user  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]

SOURCE = {
    "demo-main/app.py": b'def main():\n    """Entry point."""\n    return helper()\n\n\ndef helper():\n    return 1\n',
    "demo-main/README.md": b"# Demo\n",
}


def archive(entries: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, data in (entries or SOURCE).items():
            zipped.writestr(name, data)
    return buffer.getvalue()


async def upload_project(owner: httpx.AsyncClient, ws: str, slug: str = "demo") -> dict[str, Any]:
    created = await unsafe(
        owner,
        "POST",
        f"/api/v1/workspaces/{ws}/projects",
        json={"source": "upload", "name": "Demo", "slug": slug},
    )
    assert created.status_code == 201, created.text
    project: dict[str, Any] = created.json()
    return project


async def upload(
    owner: httpx.AsyncClient, project: str, data: bytes, content_type: str = "application/zip"
) -> httpx.Response:
    return await unsafe(
        owner,
        "POST",
        f"/api/v1/projects/{project}/uploads",
        content=data,
        headers={"Content-Type": content_type},
    )


async def test_an_uploaded_repository_is_scanned_and_built_without_github(
    client: httpx.AsyncClient, new_user: NewUser
) -> None:
    app = client.app  # type: ignore[attr-defined]
    owner = await new_user()
    ws = await workspace(owner)
    project = await upload_project(owner, ws)
    assert project["source"] == "upload" and project["tracked_ref"] == "upload"
    assert (await owner.get(f"/api/v1/workspaces/{ws}/connections")).json()[
        "items"
    ] == []  # not a GitHub connection
    no_upload = await unsafe(
        owner,
        "POST",
        f"/api/v1/projects/{project['id']}/builds",
        json={},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    assert no_upload.status_code == 409 and code(no_upload) == "NO_UPLOAD"

    data = archive()
    sent = await upload(owner, project["id"], data)
    assert sent.status_code == 202, sent.text
    snapshot = sent.json()
    assert snapshot["status"] == "pending" and len(snapshot["commit_sha"]) == 64
    key = upload_key(ws, project["id"], snapshot["id"])  # type: ignore[arg-type]
    assert await app.state.objects.exists(key)
    again = await upload(owner, project["id"], data)  # the same archive: the same snapshot
    assert again.status_code == 202 and again.json()["id"] == snapshot["id"]
    assert await drive_snapshot(app, snapshot["id"]) == ["completed"]
    scanned = (await owner.get(f"/api/v1/projects/{project['id']}/snapshots/{snapshot['id']}")).json()
    assert scanned["status"] == "scanned" and scanned["scan_summary"] == {"selected": 1, "unsupported": 1}

    build = await unsafe(
        owner,
        "POST",
        f"/api/v1/projects/{project['id']}/builds",
        json={},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    assert build.status_code == 202, build.text
    await drive(app, build.json()["id"], runtime=runtime_for(app))
    ready = (await owner.get(f"/api/v1/builds/{build.json()['id']}")).json()
    assert ready["status"] == "ready" and ready["commit_sha"] == snapshot["commit_sha"]
    tree = (await owner.get(f"/api/v1/builds/{build.json()['id']}/tree")).json()["items"]
    assert sorted(e["path"] for e in tree) == ["README.md", "app.py"]  # the shared top directory is stripped

    # A generated build from the uploaded snapshot works like any other.
    estimate = (
        await make_estimate(owner, project["id"], snapshot["id"], preset="replay-v2", guide_pages=3)
    ).json()
    generated = await start_from(owner, project["id"], estimate["id"])
    assert generated.status_code == 202, generated.text
    await drive(app, generated.json()["id"], runtime=runtime_for(app))
    assert (await owner.get(f"/api/v1/builds/{generated.json()['id']}")).json()["status"] == "ready"
    freshness = (await owner.get(f"/api/v1/projects/{project['id']}/freshness")).json()
    assert freshness["state"] == "not_tracked"


async def test_hostile_archives_are_rejected_by_the_scan(
    client: httpx.AsyncClient, new_user: NewUser
) -> None:
    app = client.app  # type: ignore[attr-defined]
    owner = await new_user()
    ws = await workspace(owner)
    project = await upload_project(owner, ws)
    for entries, rejection in (
        ({"../escape.py": b"x"}, "path_traversal"),
        ({"a/../../b.py": b"x"}, "path_traversal"),
    ):
        sent = await upload(owner, project["id"], archive(entries))
        assert sent.status_code == 202
        await drive_snapshot(app, sent.json()["id"])
        result = (await owner.get(f"/api/v1/projects/{project['id']}/snapshots/{sent.json()['id']}")).json()
        assert (result["status"], result["rejection_code"]) == ("rejected", rejection)


async def test_upload_requests_are_checked_before_anything_is_stored(
    client: httpx.AsyncClient,
    idp: FakeIdP,
    github: FakeGitHub,
    new_user: NewUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await new_user()
    ws = await workspace(owner)
    project = await upload_project(owner, ws)
    pid = project["id"]
    assert code(await upload(owner, pid, b"not a zip at all")) == "NOT_A_ZIP"
    assert (await upload(owner, pid, archive(), content_type="text/plain")).status_code == 415
    assert code(await upload(owner, pid, b"")) == "UPLOAD_EMPTY"
    monkeypatch.setattr(projects_router, "MAX_COMPRESSED_BYTES", 1024)
    big = await upload(owner, pid, archive({"big.txt": secrets.token_bytes(4096)}))
    assert big.status_code == 413
    assert ddl("SELECT count(*) FROM snapshots WHERE project_id = :p", p=pid)[0][0] == 0
    monkeypatch.undo()

    viewer = await new_user()
    await join(owner, ws, viewer, "viewer")
    assert (await upload(viewer, pid, archive())).status_code == 403
    # GitHub-only actions are refused for an uploaded project, and uploads for a GitHub project.
    snap = await unsafe(owner, "POST", f"/api/v1/projects/{pid}/snapshots", json={})
    assert snap.status_code == 409 and code(snap) == "NOT_A_GITHUB_PROJECT"
    policy = await owner.get(f"/api/v1/projects/{pid}/push-policy")
    refused = await unsafe(
        owner,
        "PUT",
        f"/api/v1/projects/{pid}/push-policy",
        json={"policy": "build"},
        headers={"If-Match": policy.headers["etag"]},
    )
    assert refused.status_code == 409 and code(refused) == "NOT_A_GITHUB_PROJECT"
    gh_owner, _gh_ws, gh_project = await setup_project(client, idp, github)
    wrong = await upload(gh_owner, gh_project, archive())
    assert wrong.status_code == 409 and code(wrong) == "NOT_AN_UPLOAD_PROJECT"
    assert uploads.UPLOAD_REF == "upload"


async def test_the_upload_rate_limit_applies_before_the_body_is_read(
    client: httpx.AsyncClient, new_user: NewUser
) -> None:
    app = client.app  # type: ignore[attr-defined]
    owner = await new_user()
    ws = await workspace(owner)
    project = await upload_project(owner, ws)
    app.state.settings = app.state.settings.model_copy(update={"rate_limit_acquisitions_per_hour": 1})
    assert (await upload(owner, project["id"], archive())).status_code == 202
    limited = await upload(owner, project["id"], b"not even a zip")  # refused by the limit, not the format
    assert limited.status_code == 429
