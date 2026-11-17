"""Baseline: require PostgreSQL 18 and create the private schema for lookup functions.

Tenant tables, RLS and role grants arrive in M1 (backlog T02), following
build-package/contracts/core_schema.sql.

Revision ID: 0001
Revises:
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIN_SERVER_VERSION_NUM = 180000  # native uuidv7()


def upgrade() -> None:
    version_num = int(op.get_bind().execute(sa.text("SHOW server_version_num")).scalar_one())
    if version_num < MIN_SERVER_VERSION_NUM:
        raise RuntimeError(f"PostgreSQL 18 or newer is required (server_version_num={version_num})")
    # Roles come from deployment/init-db.sh (or the production bootstrap), never from migrations.
    op.execute("CREATE SCHEMA app_private")
    op.execute("REVOKE ALL ON SCHEMA app_private FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA app_private TO app_runtime")


def downgrade() -> None:
    op.execute("DROP SCHEMA app_private")
