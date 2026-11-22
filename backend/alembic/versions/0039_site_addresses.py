"""Site addresses under their owner, /sites/<owner>/<project> (01 FR-030, 06): project slugs become
unique per owner, free-form site slugs become redirecting aliases, and sign-in requests name the address.
The downgrade restores the free-form slugs and runs 0036's SQL again for their reservations.

The DDL lives beside this file as reviewable SQL (0039_site_addresses.sql / _down.sql).

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0039"
down_revision: str | Sequence[str] | None = "0038"
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
    _run("0039_site_addresses.sql")


def downgrade() -> None:
    _run("0039_site_addresses_down.sql")
    _run("0036_slug_reservations.sql")
