"""A publication records the language variants its publish approved (FR-026): a site serves the published
build and exactly those variants, never one built or changed later without a new publish.

The DDL lives beside this file as reviewable SQL (0033_published_variants.sql / _down.sql).

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-21
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0033"
down_revision: str | Sequence[str] | None = "0032"
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
    _run("0033_published_variants.sql")


def downgrade() -> None:
    _run("0033_published_variants_down.sql")
