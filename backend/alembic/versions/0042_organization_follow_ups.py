"""Organization follow-ups (02 S03b, S16, 06): member details for owners and admins, handing an owner's
billing to another of its workspaces, and moving a workspace to another owner.

The DDL lives beside this file as reviewable SQL (0042_organization_follow_ups.sql / _down.sql).

Revision ID: 0042
Revises: 0041
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0042"
down_revision: str | Sequence[str] | None = "0041"
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
    _run("0042_organization_follow_ups.sql")


def downgrade() -> None:
    _run("0042_organization_follow_ups_down.sql")
