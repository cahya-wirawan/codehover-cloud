"""Async SQLAlchemy engine and sessions (04_SYSTEM_ARCHITECTURE.md "Runtime choices").

One AsyncSession per request or task; never share it between concurrent coroutines. Keep
transactions short and never hold one open across GitHub, model or object-storage calls.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.settings import Settings


def create_engine(
    settings: Settings, *, url: str | None = None, application_name: str = "api"
) -> AsyncEngine:
    """Create the process engine. Connections are opened lazily, so construction never touches the DB."""
    return create_async_engine(
        url or settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
        connect_args={"application_name": f"codehover-{application_name}", "connect_timeout": 5},
    )


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency body: one session per request, closed afterwards."""
    async with factory() as session:
        yield session
