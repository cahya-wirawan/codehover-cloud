"""Queue message (build-package/contracts/task-envelope.schema.json, v1): identifiers only, never code
or tokens. Consumers treat it as a hint and re-read the task from SQL."""

from __future__ import annotations

import secrets
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

EXCHANGE = "codehover.tasks"
QUEUE = "codehover.work.tasks"  # matches the bootstrap DLX policy pattern ^codehover\.work\.
ROUTING_KEY = "task"


class TaskEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    task_id: UUID
    build_id: UUID
    workspace_id: UUID
    trace_id: str = Field(default_factory=lambda: secrets.token_hex(16), pattern=r"^[0-9a-f]{32}$")
