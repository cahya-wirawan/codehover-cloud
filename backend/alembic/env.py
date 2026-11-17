"""Alembic environment: synchronous psycopg connection with the DDL role."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

from app.db.metadata import metadata

config = context.config


def _migration_url() -> str:
    # Programmatic callers (tests) pass the URL explicitly; the CLI reads the environment.
    explicit = config.attributes.get("migration_url")
    url = explicit or os.environ.get("MIGRATION_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Set MIGRATION_DATABASE_URL (DDL role) to run migrations")
    if not url.startswith("postgresql+psycopg://"):
        raise RuntimeError("Migration URL must use the postgresql+psycopg:// driver")
    return url


def run_migrations_offline() -> None:
    context.configure(url=_migration_url(), target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_migration_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
