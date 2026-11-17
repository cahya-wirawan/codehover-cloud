"""ORM models must mirror the migrated schema (migration SQL is authoritative)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.db import models  # noqa: F401 - registers every mapped table on the metadata
from app.db.metadata import metadata
from tests.integration.conftest import MIGRATION_URL, engine, needs_db

pytestmark = [pytest.mark.integration, needs_db]

IGNORED_TABLES = {"alembic_version"}


def db_columns() -> dict[str, dict[str, tuple[str, bool]]]:
    query = sa.text(
        "SELECT table_name, column_name, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull"
        " FROM information_schema.columns c"
        " JOIN pg_attribute a ON a.attrelid = (quote_ident(c.table_schema) || '.' ||"
        " quote_ident(c.table_name))::regclass"
        "  AND a.attname = c.column_name"
        " WHERE c.table_schema = 'public'"
    )
    tables: dict[str, dict[str, tuple[str, bool]]] = {}
    with engine(MIGRATION_URL).connect() as conn:
        for table, column, type_name, nullable in conn.execute(query):
            tables.setdefault(table, {})[column] = (type_name, nullable)
    return tables


def model_type(column: sa.Column[object]) -> str:
    compiled = column.type.compile(dialect=postgresql.dialect())
    return {
        "TIMESTAMP WITH TIME ZONE": "timestamp with time zone",
        "BYTEA": "bytea",
        "TEXT": "text",
        "UUID": "uuid",
        "JSONB": "jsonb",
        "BIGINT": "bigint",
        "INTEGER": "integer",
        "SMALLINT": "smallint",
    }.get(compiled, compiled.lower().replace("varchar", "character varying"))


def test_every_table_has_a_model_and_columns_match(migrated: None) -> None:
    actual = db_columns()
    expected = {
        table.name: {c.name: (model_type(c), bool(c.nullable)) for c in table.columns}
        for table in metadata.sorted_tables
    }
    assert set(expected) == set(actual) - IGNORED_TABLES
    mismatches = {
        table: {
            "model_only": sorted(set(cols) - set(actual[table])),
            "db_only": sorted(set(actual[table]) - set(cols)),
            "different": {
                name: (cols[name], actual[table][name])
                for name in set(cols) & set(actual[table])
                if cols[name] != actual[table][name]
            },
        }
        for table, cols in expected.items()
    }
    assert {t: m for t, m in mismatches.items() if any(m.values())} == {}
