"""Push rebuilds (M6, T21, FR-025): per-project push policy and debounced push requests.

The DDL lives beside this file as reviewable SQL (0024_push_rebuilds.sql / _down.sql) and follows
build-package/contracts/core_schema.sql, which the integration tests assert against.

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
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
    _run("0024_push_rebuilds.sql")


def downgrade() -> None:
    _run("0024_push_rebuilds_down.sql")
