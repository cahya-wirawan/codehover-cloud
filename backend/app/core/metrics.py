"""Prometheus metrics (12 "Operational stack", T18).

Labels are bounded: route templates (never raw paths), methods, status classes, stages, outcomes,
providers, purposes and scopes. No workspace, project, build, user or request IDs, no file paths, no
queries and no error text ever become label values; tenant diagnostics stay in access-controlled SQL.

Each process serves its metrics on an internal port (`METRICS_PORT`, default 9100) that the gateway
does not route and Compose does not publish. Collectors that read the database run in the scheduler
(`collect_queue_gauges`) and see aggregates only.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__

logger = logging.getLogger("app.metrics")
REGISTRY = CollectorRegistry(auto_describe=True)

_LABEL = re.compile(r"^[a-z0-9_.:-]{1,64}$")


def label(value: str | None) -> str:
    """Bound a label to a short, safe token: anything else becomes `other`."""
    return value if value and _LABEL.fullmatch(value) else "other"


INFO = Gauge("codehover_info", "Application version", ["version"], registry=REGISTRY)
INFO.labels(version=__version__).set(1)

HTTP_REQUESTS = Counter(
    "codehover_http_requests_total", "API requests", ["route", "method", "status"], registry=REGISTRY
)
HTTP_DURATION = Histogram(
    "codehover_http_request_duration_seconds",
    "API request duration (SSE excluded)",
    ["route", "method"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)
SSE_STREAMS = Gauge("codehover_sse_streams", "Open build event streams", registry=REGISTRY)
SSE_REJECTED = Counter(
    "codehover_sse_rejected_total", "Event streams refused by the stream limits", registry=REGISTRY
)
AUTH_FAILURES = Counter(
    "codehover_auth_failures_total", "Failed sign-in callbacks by reason code", ["reason"], registry=REGISTRY
)
RATE_LIMITED = Counter(
    "codehover_rate_limited_total", "Requests refused by a rate limit", ["policy"], registry=REGISTRY
)

TASKS = Counter(
    "codehover_task_outcomes_total",
    "Task attempts by stage and outcome",
    ["stage", "outcome"],
    registry=REGISTRY,
)
TASK_DURATION = Histogram(
    "codehover_task_duration_seconds",
    "Task attempt duration by stage",
    ["stage"],
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600),
    registry=REGISTRY,
)
PROVIDER_CALLS = Counter(
    "codehover_provider_calls_total",
    "Provider calls by provider, purpose and outcome",
    ["provider", "purpose", "outcome"],
    registry=REGISTRY,
)
PROVIDER_COST = Counter(
    "codehover_provider_cost_micro_usd_total", "Settled provider cost", ["provider"], registry=REGISTRY
)
PROVIDER_TOKENS = Counter(
    "codehover_provider_tokens_total", "Provider tokens", ["provider", "kind"], registry=REGISTRY
)
CACHE = Counter("codehover_generation_cache_total", "Generation cache lookups", ["result"], registry=REGISTRY)
PURGES = Counter("codehover_purges_total", "Deletion purges", ["scope", "outcome"], registry=REGISTRY)
# Owner notice emails by outcome: sent, skipped, failed, or the error code of a failed or retried send.
NOTICES = Counter("codehover_owner_notices_total", "Owner notice deliveries", ["outcome"], registry=REGISTRY)
# T23: SCIP indexer runs; a "failed" outcome means that language family fell back to name matching.
INDEXER_RUNS = Counter(
    "codehover_reference_indexer_runs_total",
    "SCIP indexer runs by indexer and outcome",
    ["indexer", "outcome"],
    registry=REGISTRY,
)
RETENTION = Counter(
    "codehover_retention_removed_total", "Items removed by retention", ["kind"], registry=REGISTRY
)
OUTBOX_PUBLISHED = Counter(
    "codehover_outbox_published_total", "Outbox notifications", ["outcome"], registry=REGISTRY
)

# Gauges the scheduler refreshes from aggregate queries (app_private.ops_gauges, 0017).
QUEUE = Gauge("codehover_queue", "Job system state (aggregates only)", ["measure"], registry=REGISTRY)


async def collect_queue_gauges(db: AsyncSession) -> dict[str, float]:
    row = (await db.execute(text("SELECT * FROM app_private.ops_gauges()"))).one()
    await db.commit()
    values = {key: float(value or 0) for key, value in row._mapping.items()}
    for key, value in values.items():
        QUEUE.labels(measure=key).set(value)
    return values


def serve(port: int | None) -> None:
    """Expose this process's metrics on an internal port (0 or None disables it)."""
    if not port:
        return
    try:
        start_http_server(port, registry=REGISTRY)
    except OSError:
        logger.warning("metrics port unavailable", extra={"port": port})


def route_of(scope: Mapping[str, Any]) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and len(path) <= 128 else "unmatched"
