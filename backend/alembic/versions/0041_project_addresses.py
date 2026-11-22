"""Changing a project's address (02 S19, 06): admins rename a project's slug, and the old address keeps
leading to it for 90 days inside the owner's namespace.

The DDL lives beside this file as reviewable SQL (0041_project_addresses.sql / _down.sql).

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0041"
down_revision: str | Sequence[str] | None = "0040"
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
    _run("0041_project_addresses.sql")


def downgrade() -> None:
    _run("0041_project_addresses_down.sql")
