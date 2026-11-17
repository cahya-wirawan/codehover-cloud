"""Liveness and readiness (06 "Endpoint inventory", 12 "Health endpoints").

live: the process and event loop answer; no dependencies. Includes the application version (23).
ready: the database answers and is at the schema head this build expects. Model-provider health is
reported separately later and never makes the API unready.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app import __version__
from app.core.errors import ApiError
from app.db.revision import current_revisions, expected_heads

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health", tags=["health"])

READY_TIMEOUT_SECONDS = 2.0


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str


class ReadyResponse(BaseModel):
    status: Literal["ready"] = "ready"
    version: str
    checks: dict[str, Literal["ok"]]


@router.get("/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    return LiveResponse(version=__version__)


@router.get(
    "/ready",
    response_model=ReadyResponse,
    responses={503: {"description": "A dependency is unavailable or the schema is not at head."}},
)
async def ready(request: Request) -> ReadyResponse:
    engine: AsyncEngine = request.app.state.engine
    failed: list[str] = []
    try:
        async with asyncio.timeout(READY_TIMEOUT_SECONDS), engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            if await current_revisions(conn) != expected_heads():
                failed.append("schema")
    except Exception as exc:  # any failure means not ready; details go to logs only
        logger.warning(
            "readiness check failed", extra={"check": "database", "error_type": type(exc).__name__}
        )
        failed.append("database")
    if failed:
        raise ApiError(503, "NOT_READY", "Service is not ready.", retryable=True, details={"failed": failed})
    return ReadyResponse(version=__version__, checks={"database": "ok", "schema": "ok"})
