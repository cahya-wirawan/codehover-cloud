"""Queue message (build-package/contracts/task-envelope.schema.json, v2): identifiers only, never code
or tokens. Consumers treat it as a hint and re-read the task from SQL.

v2 tasks belong to exactly one build or one snapshot (snapshot acquisition runs before any build).
v1 messages, which always name a build, are still accepted while they drain from the queue.
"""

from __future__ import annotations

import secrets
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

EXCHANGE = "codehover.tasks"
QUEUE = "codehover.work.tasks"  # matches the bootstrap DLX policy pattern ^codehover\.work\.
ROUTING_KEY = "task"


class TaskEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2] = 2
    task_id: UUID
    workspace_id: UUID
    build_id: UUID | None = None
    snapshot_id: UUID | None = None
    trace_id: str = Field(default_factory=lambda: secrets.token_hex(16), pattern=r"^[0-9a-f]{32}$")

    @model_validator(mode="after")
    def _one_owner(self) -> Self:
        if (self.build_id is None) == (self.snapshot_id is None):
            raise ValueError("a task belongs to exactly one build or snapshot")
        if self.schema_version == 1 and self.build_id is None:
            raise ValueError("v1 envelopes always name a build")
        return self

    def wire(self) -> str:
        """Serialized v2 payload (both owner fields present, one of them null)."""
        return self.model_copy(update={"schema_version": 2}).model_dump_json()
