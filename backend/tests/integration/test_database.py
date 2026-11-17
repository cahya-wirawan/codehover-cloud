"""PostgreSQL 18 integration: migrations, readiness and role boundaries.

Needs a database bootstrapped by deployment/init-db.sh (roles app_runtime/app_system/app_resolver):
  TEST_MIGRATION_DATABASE_URL  DDL role, e.g. postgresql+psycopg://codehover_ddl:...@localhost:5432/codehover
  TEST_DATABASE_URL            runtime role, e.g. postgresql+psycopg://app_runtime:...@localhost:5432/codehover
  TEST_SYSTEM_DATABASE_URL     system role (optional; its connect test is skipped without it)
The tests leave the database migrated to head.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from pydantic import SecretStr

from app.main import create_app
from tests.conftest import make_settings

pytestmark = pytest.mark.integration

MIGRATION_URL = os.environ.get("TEST_MIGRATION_DATABASE_URL")
RUNTIME_URL = os.environ.get("TEST_DATABASE_URL")
SYSTEM_URL = os.environ.get("TEST_SYSTEM_DATABASE_URL")

if not (MIGRATION_URL and RUNTIME_URL):
    pytest.skip("TEST_MIGRATION_DATABASE_URL and TEST_DATABASE_URL not set", allow_module_level=True)

BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture
def alembic_config() -> Config:
    config = Config(str(BACKEND / "alembic.ini"))
    config.attributes["migration_url"] = MIGRATION_URL
    return config


def ddl_engine() -> sa.Engine:
    assert MIGRATION_URL
    return sa.create_engine(MIGRATION_URL, poolclass=sa.pool.NullPool)


async def ready_status() -> httpx.Response:
    assert RUNTIME_URL
    app = create_app(make_settings(database_url=SecretStr(RUNTIME_URL)))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            return await client.get("/api/v1/health/ready")
    finally:
        await app.state.engine.dispose()


async def test_migration_cycle_and_readiness(alembic_config: Config) -> None:
    command.downgrade(alembic_config, "base")
    not_ready = await ready_status()
    assert not_ready.status_code == 503
    assert not_ready.json()["error"]["details"] == {"failed": ["schema"]}

    command.upgrade(alembic_config, "head")
    with ddl_engine().connect() as conn:
        assert conn.execute(sa.text("SELECT uuid_extract_version(uuidv7())")).scalar_one() == 7
        assert conn.execute(sa.text("SELECT to_regnamespace('app_private')")).scalar_one() is not None
        assert conn.execute(
            sa.text("SELECT has_schema_privilege('app_runtime', 'app_private', 'USAGE')")
        ).scalar_one()

    # Downgrade/upgrade must round-trip cleanly (13 "Alembic fresh/upgrade").
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")

    ready = await ready_status()
    assert ready.status_code == 200, ready.text
    assert ready.json()["checks"] == {"database": "ok", "schema": "ok"}


def test_runtime_role_cannot_run_ddl() -> None:
    assert RUNTIME_URL
    engine = sa.create_engine(RUNTIME_URL, poolclass=sa.pool.NullPool)
    with engine.connect() as conn:
        privileged = conn.execute(
            sa.text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
        )
        assert privileged.scalar_one() is False
        with pytest.raises(sa.exc.ProgrammingError):
            conn.execute(sa.text("CREATE TABLE runtime_must_not_create (id int)"))


@pytest.mark.skipif(not SYSTEM_URL, reason="TEST_SYSTEM_DATABASE_URL not set")
def test_system_role_connects_without_bypassrls() -> None:
    assert SYSTEM_URL
    engine = sa.create_engine(SYSTEM_URL, poolclass=sa.pool.NullPool)
    with engine.connect() as conn:
        flags = conn.execute(
            sa.text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert tuple(flags) == (False, False)


def test_repository_grants_migration_fails_closed(alembic_config: Config) -> None:
    """Repositories that existed before 0004 (possibly over-broad grants) must not stay granted."""
    command.downgrade(alembic_config, "0003")
    suffix = os.urandom(4).hex()
    with ddl_engine().begin() as conn:
        user = conn.execute(sa.text("INSERT INTO users(display_name) VALUES ('m') RETURNING id")).scalar_one()
        ws = conn.execute(
            sa.text("INSERT INTO workspaces(name, slug) VALUES ('Migration', :s) RETURNING id"),
            {"s": f"mig-{suffix}"},
        ).scalar_one()
        connection = conn.execute(
            sa.text(
                "INSERT INTO repository_connections(workspace_id, installation_id, github_account_id,"
                " github_account_login, connected_by) VALUES (:w, 1, 1, 'o', :u) RETURNING id"
            ),
            {"w": ws, "u": user},
        ).scalar_one()
        conn.execute(
            sa.text(
                "INSERT INTO repositories(workspace_id, connection_id, provider_repo_id, owner, name,"
                " visibility, default_branch) VALUES (:w, :c, 7, 'o', 'pre-existing', 'private', 'main')"
            ),
            {"w": ws, "c": connection},
        )
    command.upgrade(alembic_config, "head")
    with ddl_engine().connect() as conn:
        revoked = conn.execute(
            sa.text("SELECT access_revoked_at IS NOT NULL FROM repositories WHERE workspace_id = :w"),
            {"w": ws},
        ).scalar_one()
    assert revoked
