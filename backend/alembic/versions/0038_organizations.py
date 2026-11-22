"""Namespaces, user handles and organizations (01, 03, 05, 06, 09): every person and organization has a
handle in one global namespace, every workspace belongs to one, and an organization's owners and admins
manage all of its workspaces.

The DDL lives beside this file as reviewable SQL (0038_organizations.sql / _down.sql).

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0038"
down_revision: str | Sequence[str] | None = "0037"
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
    _run("0038_organizations.sql")


def downgrade() -> None:
    _run("0038_organizations_down.sql")
