"""One plan per owner (11, 0038): an organization's or a person's workspaces share one Stripe customer,
subscription, trial and monthly allowance, and the plan's limits count across them.

The DDL lives beside this file as reviewable SQL (0040_billing_accounts.sql / _down.sql).

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0040"
down_revision: str | Sequence[str] | None = "0039"
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
    _run("0040_billing_accounts.sql")


def downgrade() -> None:
    _run("0040_billing_accounts_down.sql")
