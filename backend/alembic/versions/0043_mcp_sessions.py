"""Hosted MCP follow-ups (06 "/mcp", "/oauth"): sign-in for clients named by a client ID metadata
document, and MCP sessions with a stream of site changes and resource subscriptions.

The DDL lives beside this file as reviewable SQL (0043_mcp_sessions.sql / _down.sql).

Revision ID: 0043
Revises: 0042
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0043"
down_revision: str | Sequence[str] | None = "0042"
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
    _run("0043_mcp_sessions.sql")


def downgrade() -> None:
    _run("0043_mcp_sessions_down.sql")
