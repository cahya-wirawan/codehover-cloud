"""Schema-version check for readiness: the database must be at the Alembic head this build ships."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

BACKEND_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def expected_heads() -> frozenset[str]:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    return frozenset(ScriptDirectory.from_config(config).get_heads())


async def current_revisions(conn: AsyncConnection) -> frozenset[str]:
    exists = (await conn.execute(text("SELECT to_regclass('public.alembic_version')"))).scalar_one()
    if exists is None:
        return frozenset()
    rows = await conn.execute(text("SELECT version_num FROM alembic_version"))
    return frozenset(row[0] for row in rows)
