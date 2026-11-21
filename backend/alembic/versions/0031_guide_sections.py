"""Guide pages belong to a named section of the guide, which the viewer shows as a heading (19 P01).

The DDL lives beside this file as reviewable SQL (0031_guide_sections.sql / _down.sql).

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-21
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0030"
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
    _run("0031_guide_sections.sql")


def downgrade() -> None:
    _run("0031_guide_sections_down.sql")
