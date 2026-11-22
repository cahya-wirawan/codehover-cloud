"""MCP sign-in (06 "/oauth", 09): OAuth 2.1 with PKCE lets an MCP client read a private hosted site for a
member who consents, with dynamic client registration, one-time codes, site-bound access tokens and
rotating refresh tokens.

The DDL lives beside this file as reviewable SQL (0037_mcp_oauth.sql / _down.sql).

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0037"
down_revision: str | Sequence[str] | None = "0036"
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
    _run("0037_mcp_oauth.sql")


def downgrade() -> None:
    _run("0037_mcp_oauth_down.sql")
