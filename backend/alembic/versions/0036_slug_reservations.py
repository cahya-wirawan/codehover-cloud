"""A released site address stays reserved for its workspace (05 "Published sites", 09): unpublishing,
changing the address or deleting the project or workspace no longer lets another workspace take it at once.

The DDL lives beside this file as reviewable SQL (0036_slug_reservations.sql / _down.sql).

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0036"
down_revision: str | Sequence[str] | None = "0035"
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
    _run("0036_slug_reservations.sql")


def downgrade() -> None:
    _run("0036_slug_reservations_down.sql")
