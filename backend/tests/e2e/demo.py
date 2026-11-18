"""A local demo of the whole product with fake Google and GitHub: test tooling, never part of the app.

    cd backend && set -a && . ../deployment/.env && set +a && .venv/bin/python -m tests.e2e.demo

Then open http://127.0.0.1:8770 (use 127.0.0.1, not localhost). It runs the real API and the built
frontend (`cd frontend && npm run build` first) on the E2E server, with:
- its own database (`codehover_demo`) and bucket (`codehover-demo`) on the Compose stack, so the stack's
  workers never touch demo jobs and demo data survives restarts;
- "Continue with Google" signing you in locally as a fresh demo user (no Google involved);
- two repositories connected to every workspace automatically: `demo-api` (a tiny fixture) and
  `codehover-engine` (this project's own engine package, for a bigger read);
- free replay builds only (source-only cards; no model calls).

`--reset` drops the demo database and bucket first.
"""

from __future__ import annotations

import argparse
import io
import os
import secrets
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

BACKEND = Path(__file__).resolve().parents[2]
DATABASE, BUCKET = "codehover_demo", "codehover-demo"
ENGINE = BACKEND / "app" / "engine"


def engine_tarball() -> bytes:
    """This project's engine package as a repository archive (local only; it never leaves the machine)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(ENGINE.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            data = path.read_bytes()
            info = tarfile.TarInfo(f"octo-engine-c0ffee/engine/{path.relative_to(ENGINE)}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def prepare(reset: bool) -> None:
    """Create (or, with reset, recreate) the demo database and bucket, migrate, and point the server at them."""
    import boto3

    from tests.e2e.restore_drill import prepare as create_database
    from tests.e2e.restore_drill import sql, storage_env

    env = {**storage_env(), "S3_BUCKET": BUCKET}
    s3 = boto3.client(
        "s3",
        endpoint_url=env["S3_ENDPOINT_URL"],
        aws_access_key_id=env["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=env["S3_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )
    buckets = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
    if reset:
        sql("postgres", f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
        if BUCKET in buckets:
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if keys:
                    s3.delete_objects(Bucket=BUCKET, Delete={"Objects": keys})
            s3.delete_bucket(Bucket=BUCKET)
            buckets.discard(BUCKET)
    if not sql("postgres", f"SELECT 1 FROM pg_database WHERE datname = '{DATABASE}'"):  # noqa: S608 - constant
        create_database(DATABASE)
    if BUCKET not in buckets:
        s3.create_bucket(Bucket=BUCKET)
    ddl = f"postgresql+psycopg://codehover_ddl:{os.environ['POSTGRES_PASSWORD']}@127.0.0.1:55432/{DATABASE}"
    subprocess.run(  # noqa: S603 - fixed command
        [str(BACKEND / ".venv/bin/alembic"), "upgrade", "head"],
        cwd=BACKEND,
        check=True,
        env={**os.environ, "MIGRATION_DATABASE_URL": ddl},
        capture_output=True,
    )
    runtime = f"postgresql+psycopg://app_runtime:{os.environ['APP_DB_PASSWORD']}@127.0.0.1:55432/{DATABASE}"
    # Read when the server and the test helpers are imported, so set before demo_app().
    os.environ.update({**env, "TEST_MIGRATION_DATABASE_URL": ddl, "TEST_DATABASE_URL": runtime})


def demo_app(port: int) -> Any:
    from fastapi import Request
    from starlette.responses import Response

    from tests.e2e import server
    from tests.integration.auth_support import ddl
    from tests.integration.test_builds import fixture_tarball

    app = server.build(port)
    _idp, gh = app.state.e2e_fakes
    tarballs = {"demo-api": fixture_tarball(), "codehover-engine": engine_tarball()}

    def register(installation: int, repos: list[tuple[int, str]]) -> None:
        gh.repos[installation] = [
            {
                "id": rid,
                "name": name,
                "owner": {"login": "octo-org"},
                "private": True,
                "visibility": "private",
                "default_branch": "main",
            }
            for rid, name in repos
        ]
        for rid, name in repos:
            gh.tarballs[rid] = tarballs.get(name, tarballs["demo-api"])

    # Connections stored by earlier runs: make the fake GitHub know them again.
    known: dict[int, list[tuple[int, str]]] = {}
    for installation, rid, name in ddl(
        "SELECT c.installation_id, r.provider_repo_id, r.name FROM repository_connections c"
        " JOIN repositories r ON r.connection_id = c.id"
    ):
        known.setdefault(int(installation), []).append((int(rid), name))
    for installation, repos in known.items():
        register(installation, repos)

    def ensure_connection(workspace_id: str) -> None:
        if ddl("SELECT 1 FROM repository_connections WHERE workspace_id = :w", w=workspace_id):
            return
        owner = ddl(
            "SELECT user_id FROM memberships WHERE workspace_id = :w AND role = 'owner' LIMIT 1",
            w=workspace_id,
        )
        if not owner:
            return
        installation = secrets.randbelow(10**9) + 1
        repos = [(secrets.randbelow(10**9) + 1, name) for name in tarballs]
        register(installation, repos)
        connection = ddl(
            "INSERT INTO repository_connections(workspace_id, installation_id, github_account_id, github_account_login,"
            " connected_by) VALUES (:w, :i, 77, 'octo-org', :u) RETURNING id",
            w=workspace_id,
            i=installation,
            u=owner[0][0],
        )[0][0]
        for rid, name in repos:
            ddl(
                "INSERT INTO repositories(workspace_id, connection_id, provider_repo_id, owner, name, visibility,"
                " default_branch) VALUES (:w, :c, :r, 'octo-org', :n, 'private', 'main')",
                w=workspace_id,
                c=connection,
                r=rid,
                n=name,
            )

    @app.middleware("http")
    async def demo(request: Request, call_next: Any) -> Response:
        parts = request.url.path.split("/")
        # /api/v1/workspaces/{wid}/... : every workspace comes with the demo repositories.
        if len(parts) > 5 and parts[1:4] == ["api", "v1", "workspaces"] and request.method == "GET":
            try:
                from uuid import UUID

                ensure_connection(str(UUID(parts[4])))
            except ValueError:
                pass
        response: Response = await call_next(request)
        location = response.headers.get("location", "")
        if request.url.path == "/api/v1/auth/google/start" and location.startswith(
            "https://accounts.google.com"
        ):
            # Sign in locally: the fake provider's consent page instead of Google.
            response.headers["location"] = f"/__e2e__/google?{urlsplit(location).query}"
        return response

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Local demo with fake Google and GitHub.")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--reset", action="store_true", help="drop the demo database and bucket first")
    args = parser.parse_args()
    if not (BACKEND.parent / "frontend" / "dist" / "index.html").exists():
        print("build the frontend first: cd frontend && npm run build", file=sys.stderr)
        return 2
    prepare(args.reset)
    import uvicorn

    print(f"codehover demo: http://127.0.0.1:{args.port}  (Ctrl+C to stop)", flush=True)
    uvicorn.run(demo_app(args.port), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
