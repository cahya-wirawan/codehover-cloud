"""Engine output (symbols, reference edges), job notifications and GitHub webhook receipts (T07-T09).

The DDL lives beside this file as reviewable SQL (0005_engine_jobs_webhooks.sql / _down.sql) and follows
build-package/contracts/core_schema.sql, which the integration tests assert against.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
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
    _run("0005_engine_jobs_webhooks.sql")


def downgrade() -> None:
    _run("0005_engine_jobs_webhooks_down.sql")
