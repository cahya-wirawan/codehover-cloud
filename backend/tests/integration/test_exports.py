"""Static ZIP and knowledge-base exports with authorized downloads (T16; 06, 10, 13 "export", A19).

M3 exit criterion: export tests pass. No real provider or GitHub is called.
"""

from __future__ import annotations

import io
import json
import re
import secrets
import zipfile
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

from app.engine import generate
from tests.integration.auth_support import FakeIdP, ddl, unsafe
from tests.integration.conftest import needs_db
from tests.integration.test_builds import drive, setup_project
from tests.integration.test_generation import generation_build, runtime_for
from tests.integration.test_github import FakeGitHub, github  # noqa: F401
from tests.integration.test_workspaces import NewUser, join, new_user  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]
SECRETS = (b"hunter2", b"AKIAABCDEFGHIJKLMNOP", b"SECRET_TOKEN", b"not really a key")
ENTRIES = {
    "index.html",
    "assets/viewer.js",
    "assets/viewer.css",
    "data/data.js",
    "data/kb.json",
    "NOTICE.txt",
    "manifest.json",
}


async def request(person: httpx.AsyncClient, build: str, fmt: str) -> httpx.Response:
    return await unsafe(
        person,
        "POST",
        f"/api/v1/builds/{build}/exports",
        json={"format": fmt},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )


async def exported(owner: httpx.AsyncClient, app: Any, build: str, fmt: str) -> tuple[str, httpx.Response]:
    created = await request(owner, build, fmt)
    assert created.status_code == 202, created.text
    assert created.json()["status"] == "pending"
    assert await drive(app, build, runtime=runtime_for(app)) == ["completed"]
    status = (await owner.get(f"/api/v1/exports/{created.json()['id']}")).json()
    assert status["status"] == "ready" and status["expires_at"] and status["byte_count"] > 0
    return created.json()["id"], await owner.get(f"/api/v1/exports/{created.json()['id']}/download")


async def ready(
    client: httpx.AsyncClient, idp: FakeIdP, gh: FakeGitHub
) -> tuple[httpx.AsyncClient, str, str, Any]:
    owner, ws, build, app = await generation_build(client, idp, gh, preset="replay-v2")
    await drive(app, build, runtime=runtime_for(app))
    return owner, ws, build, app


async def test_static_zip_is_self_contained_and_contains_only_shown_source(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, build, app = await ready(client, idp, github)
    export_id, download = await exported(owner, app, build, "static_zip")
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    assert re.fullmatch(
        r'attachment; filename="codehover-api-[0-9a-f]{12}\.zip"', download.headers["content-disposition"]
    )
    assert download.headers["cache-control"] == "no-store"
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    assert set(archive.namelist()) == ENTRIES  # fixed names: nothing from the repository becomes a file name
    for name in ENTRIES:
        body = archive.read(name)
        assert not any(secret in body for secret in SECRETS), (
            name
        )  # A19/A07: excluded and blocked content absent
        if name != "data/kb.json" and name != "data/data.js":
            assert b"http://" not in body and b"https://" not in body, name  # nothing loads from the network
    index = archive.read("index.html").decode()
    assert (
        "Content-Security-Policy" in index and "default-src 'none'" in index and "script-src 'self'" in index
    )
    assert re.findall(r'(?:src|href)="([^"#]+)"', index) and all(
        ref in ENTRIES for ref in re.findall(r'(?:src|href)="([^"#]+)"', index)
    )  # every referenced asset is in the archive
    kb = json.loads(archive.read("data/kb.json"))
    assert not list(Draft202012Validator(generate.schema("kb-export")).iter_errors(kb))
    paths = {f["path"] for f in kb["files"]}
    assert "src/app.py" in paths and "README.md" in paths
    assert not paths & {
        ".env",
        "deploy/key.pem",
        "src/settings.py",
    }  # excluded/blocked files are not listed at all
    assert any(s["name"] == "main" and s["card"]["summary"] == "Entry point." for s in kb["symbols"])
    assert kb["guide"] and all(page["claims"] for page in kb["guide"])
    manifest = json.loads(archive.read("manifest.json"))
    import hashlib

    assert all(hashlib.sha256(archive.read(n)).hexdigest() == h for n, h in manifest["entries"].items())
    data_js = archive.read("data/data.js").decode()
    assert data_js.startswith("window.CODEHOVER_DATA = ") and "</script" not in data_js.lower()

    second_id, second = await exported(owner, app, build, "static_zip")
    assert second_id != export_id and second.content == download.content  # deterministic archives


async def test_kb_json_export_validates_against_its_contract(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, build, app = await ready(client, idp, github)
    _id, download = await exported(owner, app, build, "kb_json")
    assert download.headers["content-type"] == "application/json"
    assert download.headers["content-disposition"].endswith('-kb.json"')
    kb = download.json()
    assert kb["schema"] == "urn:codehover-cloud:schema:kb-export:1.0.0"
    assert not list(Draft202012Validator(generate.schema("kb-export")).iter_errors(kb))
    assert kb["generator"]["preset"] == "replay-v2" and kb["build"]["commit_sha"] == github.commit_sha
    assert not any(secret in download.content for secret in SECRETS)


async def test_only_the_requester_or_an_admin_may_download_and_packages_expire(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, new_user: NewUser
) -> None:
    owner, ws, build, app = await ready(client, idp, github)
    editor = await new_user()
    await join(owner, ws, editor, "editor")
    viewer = await new_user()
    await join(owner, ws, viewer, "viewer")
    assert (
        await request(viewer, build, "kb_json")
    ).status_code == 403  # exporting needs the editor role (09)
    created = await request(editor, build, "kb_json")
    assert created.status_code == 202
    await drive(app, build, runtime=runtime_for(app))
    export_id = created.json()["id"]
    assert (await editor.get(f"/api/v1/exports/{export_id}/download")).status_code == 200  # the requester
    assert (await owner.get(f"/api/v1/exports/{export_id}/download")).status_code == 200  # an admin (owner)
    other = await new_user()
    await join(owner, ws, other, "editor")
    assert (
        await other.get(f"/api/v1/exports/{export_id}")
    ).status_code == 404  # neither: same 404 as a stranger
    stranger, _, _ = await setup_project(client, idp, github)
    assert (await stranger.get(f"/api/v1/exports/{export_id}/download")).status_code == 404
    ddl("UPDATE exports SET expires_at = now() - interval '1 minute' WHERE id = :e", e=export_id)
    expired = await editor.get(f"/api/v1/exports/{export_id}/download")
    assert expired.status_code == 410 and expired.json()["error"]["code"] == "EXPORT_EXPIRED"
    audit = ddl(
        "SELECT action FROM audit_events WHERE workspace_id = :w AND action LIKE 'export.%' ORDER BY created_at",
        w=ws,
    )
    assert [a[0] for a in audit] == ["export.requested", "export.downloaded", "export.downloaded"]


async def test_exports_need_a_ready_build_and_respect_the_hourly_quota(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, build, app = await generation_build(client, idp, github, preset="replay-v2")
    not_ready = await request(owner, build, "static_zip")
    assert not_ready.status_code == 409 and not_ready.json()["error"]["code"] == "BUILD_NOT_READY"
    await drive(app, build, runtime=runtime_for(app))
    user = ddl("SELECT created_by FROM builds WHERE id = :b", b=build)[0][0]
    for _ in range(10):
        ddl(
            "INSERT INTO exports(workspace_id, build_id, format, requested_by) VALUES (:w, :b, 'kb_json', :u)",
            w=ws,
            b=build,
            u=user,
        )
    limited = await request(owner, build, "kb_json")
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "EXPORT_LIMIT"
    assert (await owner.get(f"/api/v1/builds/{build}")).json()[
        "status"
    ] == "ready"  # exports never touch the build
