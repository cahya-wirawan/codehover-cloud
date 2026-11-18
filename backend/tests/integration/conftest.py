"""Shared fixtures for PostgreSQL 18 integration tests (see test_database.py for the env variables)."""

from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

if TYPE_CHECKING:
    from tests.integration.auth_support import FakeIdP

MIGRATION_URL = os.environ.get("TEST_MIGRATION_DATABASE_URL")
RUNTIME_URL = os.environ.get("TEST_DATABASE_URL")
SYSTEM_URL = os.environ.get("TEST_SYSTEM_DATABASE_URL")
BACKEND = Path(__file__).resolve().parents[2]

needs_db = pytest.mark.skipif(
    not (MIGRATION_URL and RUNTIME_URL), reason="TEST_MIGRATION_DATABASE_URL and TEST_DATABASE_URL not set"
)
needs_system = pytest.mark.skipif(not SYSTEM_URL, reason="TEST_SYSTEM_DATABASE_URL not set")


def alembic_config() -> Config:
    config = Config(str(BACKEND / "alembic.ini"))
    config.attributes["migration_url"] = MIGRATION_URL
    return config


def engine(url: str | None) -> sa.Engine:
    assert url
    return sa.create_engine(url, poolclass=sa.pool.NullPool)


def digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


class Db:
    """One connection in one transaction, with helpers to set RLS context like the application does."""

    def __init__(self, conn: sa.Connection) -> None:
        self.conn = conn

    def context(self, *, workspace: UUID | None = None, user: UUID | None = None) -> None:
        self.conn.execute(
            sa.text("SELECT set_config('app.workspace_id', :w, true), set_config('app.user_id', :u, true)"),
            {"w": str(workspace) if workspace else "", "u": str(user) if user else ""},
        )

    def one(self, sql: str, **params: Any) -> Any:
        return self.conn.execute(sa.text(sql), params).scalar_one()

    def rows(self, sql: str, **params: Any) -> list[sa.Row[Any]]:
        return list(self.conn.execute(sa.text(sql), params))

    def run(self, sql: str, **params: Any) -> None:
        self.conn.execute(sa.text(sql), params)


@dataclass(frozen=True)
class Tenant:
    user: UUID
    workspace: UUID
    connection: UUID
    repository: UUID
    project: UUID
    snapshot: UUID
    build: UUID
    task: UUID
    outbox: UUID


@dataclass(frozen=True)
class World:
    a: Tenant
    b: Tenant


def _make_tenant(db: Db, label: str, run: str) -> Tenant:
    user = db.one(
        "SELECT app_private.create_user_with_identity('github', 'https://github.com', :sub, :name, :mail)",
        sub=f"{run}-{label}",
        name=f"User {label}",
        mail=f"{label}-{run}@example.test",
    )
    db.context(user=user)
    workspace = db.one("SELECT app_private.create_workspace(:n, :s)", n=f"Team {label}", s=f"t-{label}-{run}")
    db.context(workspace=workspace, user=user)
    connection = db.one(
        "INSERT INTO repository_connections(workspace_id, installation_id,"
        " github_account_id, github_account_login,"
        " connected_by) VALUES (:w, :inst, 77, 'octo', :u) RETURNING id",
        w=workspace,
        inst=secrets.randbelow(10**12) + 1,
        u=user,
    )
    repository = db.one(
        "INSERT INTO repositories(workspace_id, connection_id, provider_repo_id, owner, name, visibility,"
        " default_branch) VALUES (:w, :c, :rid, 'octo', 'repo', 'private', 'main') RETURNING id",
        w=workspace,
        c=connection,
        rid=secrets.randbelow(10**12) + 1,
    )
    project = db.one(
        "INSERT INTO projects(workspace_id, repository_id, name, slug, tracked_ref)"
        " VALUES (:w, :r, 'Project', 'project', 'main') RETURNING id",
        w=workspace,
        r=repository,
    )
    snapshot = db.one(
        "INSERT INTO snapshots(workspace_id, project_id, commit_sha, observed_ref, status)"
        " VALUES (:w, :p, :sha, 'main', 'scanned') RETURNING id",
        w=workspace,
        p=project,
        sha=secrets.token_hex(20),
    )
    build = db.one(
        "INSERT INTO builds(workspace_id, project_id, snapshot_id, config, config_digest, engine_version,"
        " prompt_version, cap_micro_usd, created_by)"
        " VALUES (:w, :p, :s, '{}', :d, '0.1.0', '0.1.0', 1000, :u) RETURNING id",
        w=workspace,
        p=project,
        s=snapshot,
        d=secrets.token_hex(32),
        u=user,
    )
    task = db.one(
        "INSERT INTO build_tasks(workspace_id, build_id, stage, work_key, state, next_attempt_at, output_key)"
        " VALUES (:w, :b, 'explain', 'f1', 'retry_wait', now() - interval '1 minute', 'secret-output')"
        " RETURNING id",
        w=workspace,
        b=build,
    )
    outbox = db.one(
        "INSERT INTO outbox(workspace_id, aggregate_id, event_type, payload)"
        " VALUES (:w, :t, 'task.ready', '{}') RETURNING id",
        w=workspace,
        t=task,
    )
    db.context()
    return Tenant(user, workspace, connection, repository, project, snapshot, build, task, outbox)


@pytest.fixture(scope="module")
def migrated() -> None:
    command.upgrade(alembic_config(), "head")


@pytest.fixture
def world(migrated: None) -> Iterator[World]:
    """Two tenants created through the runtime role, committed (other roles must see them)."""
    run = secrets.token_hex(4)
    with engine(RUNTIME_URL).begin() as conn:
        db = Db(conn)
        world = World(a=_make_tenant(db, "a", run), b=_make_tenant(db, "b", run))
    yield world


@pytest.fixture
def runtime() -> Iterator[Db]:
    """A rolled-back runtime transaction: assertions here never leave data behind."""
    with engine(RUNTIME_URL).connect() as conn, conn.begin() as tx:
        yield Db(conn)
        tx.rollback()


@pytest.fixture
def system() -> Iterator[Db]:
    with engine(SYSTEM_URL).connect() as conn, conn.begin() as tx:
        yield Db(conn)
        tx.rollback()


@pytest.fixture
def ddl() -> Iterator[Db]:
    with engine(MIGRATION_URL).connect() as conn, conn.begin() as tx:
        yield Db(conn)
        tx.rollback()


# ---------------------------------------------------------------- sign-in fixtures (see auth_support.py)
@pytest.fixture
def idp() -> FakeIdP:
    from tests.integration.auth_support import FakeIdP

    return FakeIdP()


@pytest.fixture
async def client(idp: FakeIdP, migrated: None) -> AsyncIterator[httpx.AsyncClient]:
    from tests.integration.auth_support import browser, make_app

    app, mock_http = make_app(idp)
    async with browser(app) as c:
        c.app = app  # type: ignore[attr-defined]  # lets tests open more browsers on the same app
        yield c
    await mock_http.aclose()
    await app.state.engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _no_live_workers_on_the_test_database() -> None:
    """Live workers (e.g. a running Compose stack on the same database) consume test notifications and
    make job tests flaky. Fail fast with a clear message instead."""
    if not MIGRATION_URL:
        return
    with engine(MIGRATION_URL).connect() as conn:
        live = (
            conn.execute(
                sa.text(
                    "SELECT DISTINCT application_name FROM pg_stat_activity"
                    " WHERE datname = current_database()"
                    " AND application_name IN ('codehover-worker', 'codehover-dispatcher', 'codehover-scheduler')"
                )
            )
            .scalars()
            .all()
        )
    if live:
        pytest.exit(
            f"Live codehover processes are connected to the test database ({', '.join(sorted(live))}). "
            "Stop them (docker compose ... stop worker dispatcher scheduler) or use a separate database.",
            returncode=2,
        )
