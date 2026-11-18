"""Repository grants: only repositories the connecting user can read (T05 security review).

The DDL lives beside this file as reviewable SQL (0004_repository_grants.sql / _down.sql) and follows
build-package/contracts/core_schema.sql, which the integration tests assert against.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HERE = Path(__file__).resolve().parent


def _run(name: str) -> None:
    # Raw DB-API cursor with no parameters: psycopg then sends the file as one simple-query batch
    # (multiple statements, DO blocks) and does not parse `%` in LIKE patterns as placeholders.
    # It runs inside Alembic's migration transaction, so a failure rolls back completely.
    cursor = op.get_bind().connection.cursor()
    try:
        cursor.execute((HERE / name).read_text())
    finally:
        cursor.close()


def upgrade() -> None:
    _run("0004_repository_grants.sql")


def downgrade() -> None:
    _run("0004_repository_grants_down.sql")
