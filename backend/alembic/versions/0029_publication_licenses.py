"""Public sites of public-source repositories need a license that permits redistribution (FR-029).

The DDL lives beside this file as reviewable SQL (0029_publication_licenses.sql / _down.sql) and follows
build-package/contracts/core_schema.sql, which the integration tests assert against.

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0029"
down_revision: str | Sequence[str] | None = "0028"
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
    _run("0029_publication_licenses.sql")


def downgrade() -> None:
    _run("0029_publication_licenses_down.sql")
