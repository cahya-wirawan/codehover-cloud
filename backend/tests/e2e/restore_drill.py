"""Restore drill on the local Compose stack (22 "Recovery", 18 release checklist, A22, NFR-009).

Test tooling, run from `backend/` with `deployment/.env` sourced and the stack's db and objectstore up:

    python -m tests.e2e.restore_drill

It never touches the stack's own database or bucket. In a scratch database and bucket it:
1. seeds two published replay sites through the E2E server (fake Google/GitHub, real S3);
2. takes the backup (`pg_dump -Fc` inside the db container);
3. deletes one project and purges it, so the backup is older than the deletion;
4. restores the backup into a second database, then runs `tombstones reapply`, `tombstones purge` and
   `verify_restore` with the runtime role, exactly as the runbook says, and times each step (RTO);
5. checks that the deleted project stays deleted with no source resurrected, and that the kept site's
   database rows and objects agree; then drops the scratch databases and empties the bucket.
Prints a JSON report; exits non-zero if any check fails.
"""

# ruff: noqa: S603, S608, ASYNC220, ASYNC221
# Local test tooling: fixed commands, and SQL built only from UUIDs our own API returned (checked below).
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

BACKEND = Path(__file__).resolve().parents[2]
DEPLOYMENT = BACKEND.parent / "deployment"
SOURCE, RESTORED, BUCKET = "codehover_drill_src", "codehover_drill_restored", "codehover-drill"
PORT = 8767


def compose(*args: str, stdin: bytes | None = None) -> bytes:
    command = ["docker", "compose", "--env-file", ".env", "-f", "compose.yaml", *args]
    return subprocess.run(command, cwd=DEPLOYMENT, input=stdin, check=True, capture_output=True).stdout


def sql(database: str, statement: str) -> str:
    return (
        compose(
            "exec",
            "-T",
            "db",
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "codehover_ddl",
            "-d",
            database,
            "-Atc",
            statement,
        )
        .decode()
        .strip()
    )


def prepare(database: str) -> None:
    sql("postgres", f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")
    sql("postgres", f"CREATE DATABASE {database}")
    # The per-database part of deployment/init-db.sh (roles are cluster-wide and exist already).
    sql(
        database,
        f"REVOKE ALL ON DATABASE {database} FROM PUBLIC;"
        f" GRANT CONNECT ON DATABASE {database} TO app_runtime, app_system;"
        " GRANT USAGE ON SCHEMA public TO app_runtime, app_system, app_resolver;"
        " ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_runtime;"
        " ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO app_runtime",
    )


def urls(database: str) -> tuple[str, str]:
    ddl = f"postgresql+psycopg://codehover_ddl:{os.environ['POSTGRES_PASSWORD']}@127.0.0.1:55432/{database}"
    runtime = f"postgresql+psycopg://app_runtime:{os.environ['APP_DB_PASSWORD']}@127.0.0.1:55432/{database}"
    return ddl, runtime


def storage_env() -> dict[str, str]:
    return {
        "S3_ENDPOINT_URL": f"http://127.0.0.1:{os.environ.get('S3_HOST_PORT', '8333')}",
        "S3_ACCESS_KEY_ID": os.environ["S3_ROOT_USER"],
        "S3_SECRET_ACCESS_KEY": os.environ["S3_ROOT_PASSWORD"],
        "S3_BUCKET": BUCKET,
    }


def bucket(action: str) -> None:
    import boto3

    env = storage_env()
    s3 = boto3.client(
        "s3",
        endpoint_url=env["S3_ENDPOINT_URL"],
        aws_access_key_id=env["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=env["S3_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )
    existing = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
    if BUCKET in existing:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                s3.delete_objects(Bucket=BUCKET, Delete={"Objects": keys})
        if action == "drop":
            s3.delete_bucket(Bucket=BUCKET)
    if action == "create":
        s3.create_bucket(Bucket=BUCKET)


def tool(module: str, *args: str, database: str) -> tuple[float, str]:
    """Run an ops command as the runtime role against `database`, as the runbook does."""
    _ddl, runtime = urls(database)
    env = {
        **os.environ,
        **storage_env(),
        "DATABASE_URL": runtime,
        "APP_ENV": "test",
        "SESSION_SECRET": "d" * 32,
        "APP_ENCRYPTION_KEY": "e" * 32,
    }
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-m", module, *args], cwd=BACKEND, env=env, capture_output=True, text=True
    )
    elapsed = time.monotonic() - started
    if done.returncode not in (0, 1):
        raise RuntimeError(f"{module} {args}: {done.stderr[-800:]}")
    return elapsed, done.stdout


def result(output: str) -> Any:
    """A tool's result: its whole output, or its last line when JSON log lines precede it."""
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return json.loads(output.strip().splitlines()[-1])


async def delete_project(base: str, cookies: httpx.Cookies, project: str) -> None:
    async with httpx.AsyncClient(base_url=base, cookies=cookies) as http:
        token = (await http.get("/api/v1/auth/csrf")).json()["csrf_token"]
        deleted = await http.post(
            f"/api/v1/projects/{project}/deletion",
            json={"confirmation": "API"},
            headers={"Origin": base, "X-CSRF-Token": token},
        )
        assert deleted.status_code == 202, deleted.text


async def drill() -> dict[str, Any]:
    from tests.e2e import seed_site

    report: dict[str, Any] = {"checks": {}}
    prepare(SOURCE)
    ddl, runtime = urls(SOURCE)
    subprocess.run(
        [str(BACKEND / ".venv/bin/alembic"), "upgrade", "head"],
        cwd=BACKEND,
        check=True,
        env={**os.environ, "MIGRATION_DATABASE_URL": ddl},
        capture_output=True,
    )
    bucket("create")
    server_env = {
        **os.environ,
        **storage_env(),
        "TEST_DATABASE_URL": runtime,
        "TEST_MIGRATION_DATABASE_URL": ddl,
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "tests.e2e.server", "--port", str(PORT)],
        cwd=BACKEND,
        env=server_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{PORT}"
    try:
        async with httpx.AsyncClient() as probe:
            for _ in range(120):
                try:
                    if (await probe.get(f"{base}/api/v1/health/live")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
        kept = await seed_site.seed(base, 0, 0, False, 300)
        doomed, cookies = await seed_site.seed_with_session(base)
        doomed["project_id"] = str(UUID(doomed["project_id"]))  # the only value placed into SQL text
        report["kept_site"], report["deleted_project"] = kept["slug"], doomed["project_id"]

        # 2. The backup, taken before the deletion.
        started = time.monotonic()
        dump = compose("exec", "-T", "db", "pg_dump", "-U", "codehover_ddl", "-Fc", SOURCE)
        report["backup_seconds"] = round(time.monotonic() - started, 2)
        report["backup_bytes"] = len(dump)

        # 3. The deletion happens after the backup and is purged (objects leave the bucket).
        await delete_project(base, cookies, doomed["project_id"])
        sql(
            SOURCE,
            f"UPDATE deletion_requests SET not_before = now() WHERE project_id = '{doomed['project_id']}'",
        )
    finally:
        server.terminate()
        server.wait(timeout=30)
    _, purged = tool("app.ops.tombstones", "purge", database=SOURCE)
    report["source_purge"] = result(purged)

    # 4. Restore into a fresh database and bring it to a servable state.
    timings: dict[str, float] = {}
    started = time.monotonic()
    prepare(RESTORED)
    compose(
        "exec",
        "-T",
        "db",
        "pg_restore",
        "-U",
        "codehover_ddl",
        "-d",
        RESTORED,
        "--no-owner",
        "--role=codehover_ddl",
        stdin=dump,
    )
    timings["restore"] = time.monotonic() - started
    report["restored_before_reapply"] = {
        "project_deleted": sql(
            RESTORED, f"SELECT deleted_at IS NOT NULL FROM projects WHERE id = '{doomed['project_id']}'"
        ),
        "content_rows": int(
            sql(
                RESTORED,
                f"SELECT count(*) FROM source_files f JOIN snapshots s ON s.id = f.snapshot_id WHERE s.project_id = '{doomed['project_id']}'",
            )
        ),
    }
    timings["reapply"], reapplied = tool("app.ops.tombstones", "reapply", database=RESTORED)
    timings["purge"], purged = tool("app.ops.tombstones", "purge", database=RESTORED)
    timings["verify"], verified = tool("app.ops.verify_restore", database=RESTORED)
    report["reapply"], report["purge"], report["verify"] = (
        result(reapplied),
        result(purged),
        result(verified),
    )
    report["purge_errors"] = {
        "source": sql(SOURCE, "SELECT string_agg(COALESCE(error_code, ''), ',') FROM deletion_requests"),
        "restored": sql(RESTORED, "SELECT string_agg(COALESCE(error_code, ''), ',') FROM deletion_requests"),
    }
    report["timings_seconds"] = {k: round(v, 2) for k, v in timings.items()}
    report["rto_seconds"] = round(sum(timings.values()), 2)

    checks = report["checks"]
    checks["backup_predates_deletion"] = (
        report["restored_before_reapply"]["project_deleted"] == "f"
        and report["restored_before_reapply"]["content_rows"] > 0
    )
    checks["tombstone_reapplied"] = report["reapply"]["applied"] >= 1 and report["reapply"]["invalid"] == 0
    checks["deleted_stays_deleted"] = (
        sql(RESTORED, f"SELECT deleted_at IS NOT NULL FROM projects WHERE id = '{doomed['project_id']}'")
        == "t"
    )
    checks["no_source_resurrected"] = (
        int(
            sql(
                RESTORED,
                f"SELECT count(*) FROM source_files f JOIN snapshots s ON s.id = f.snapshot_id WHERE s.project_id = '{doomed['project_id']}'",
            )
        )
        == 0
    )
    checks["kept_site_consistent"] = (
        report["verify"]["ok"] and report["verify"]["artifacts"] > 0 and report["verify"]["blobs"] > 0
    )
    checks["rto_under_4h"] = report["rto_seconds"] < 4 * 3600
    report["passed"] = all(checks.values())
    return report


def cleanup() -> None:
    for database in (SOURCE, RESTORED):
        sql("postgres", f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")
    bucket("drop")


def main() -> int:
    try:
        report = asyncio.run(drill())
    finally:
        cleanup()
    print(json.dumps(report, indent=1))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
