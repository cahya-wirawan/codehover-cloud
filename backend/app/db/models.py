"""ORM mappings for migration 0002 (tenant core).

The migration SQL is authoritative: constraints, RLS, grants and functions live there. These
mappings mirror its columns, and tests/integration/test_schema.py fails if they drift.
IDs and timestamps are database-generated: insert, then flush/RETURNING before using an id (05).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    Double,
    ForeignKey,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.metadata import Base

UUIDV7 = text("uuidv7()")
NOW = text("now()")


def pk() -> Mapped[UUID]:
    return mapped_column(PG_UUID(as_uuid=True), primary_key=True, server_default=UUIDV7)


def ref(target: str | None = None, *, nullable: bool = False) -> Mapped[Any]:
    args = (ForeignKey(target),) if target else ()
    return mapped_column(PG_UUID(as_uuid=True), *args, nullable=nullable)


def ts(*, nullable: bool = False, default_now: bool = False) -> Mapped[Any]:
    return mapped_column(
        DateTime(timezone=True), nullable=nullable, server_default=NOW if default_now else None
    )


def jsonb(*, default: str | None = None) -> Mapped[Any]:
    return mapped_column(JSONB, nullable=False, server_default=text(default) if default else None)


# ---------------------------------------------------------------- identity
class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = pk()
    display_name: Mapped[str] = mapped_column(String(120))
    primary_email: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    created_at: Mapped[datetime] = ts(default_now=True)
    updated_at: Mapped[datetime] = ts(default_now=True)
    deleted_at: Mapped[datetime | None] = ts(nullable=True)


class OAuthIdentity(Base):
    __tablename__ = "oauth_identities"
    id: Mapped[UUID] = pk()
    user_id: Mapped[UUID] = ref("users.id")
    provider: Mapped[str] = mapped_column(Text)
    issuer: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    verified_email: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = ts(default_now=True)


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[UUID] = pk()
    user_id: Mapped[UUID] = ref("users.id")
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    csrf_hash: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = ts(default_now=True)
    expires_at: Mapped[datetime] = ts()
    last_seen_at: Mapped[datetime] = ts(default_now=True)
    reauthenticated_at: Mapped[datetime] = ts(default_now=True)
    revoked_at: Mapped[datetime | None] = ts(nullable=True)


class OAuthTransaction(Base):
    """Reached only through app_private.oauth_transaction_create/consume, never directly."""

    __tablename__ = "oauth_transactions"
    id: Mapped[UUID] = pk()
    state_hash: Mapped[bytes] = mapped_column(LargeBinary)
    browser_binding_hash: Mapped[bytes] = mapped_column(LargeBinary)
    provider: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    nonce_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    encrypted_pkce_verifier: Mapped[bytes | None] = mapped_column(LargeBinary)
    return_path: Mapped[str] = mapped_column(Text, server_default=text("'/'"))
    initiating_session_id: Mapped[UUID | None] = ref("sessions.id", nullable=True)
    workspace_id: Mapped[UUID | None] = ref(nullable=True)
    created_at: Mapped[datetime] = ts(default_now=True)
    expires_at: Mapped[datetime] = ts()
    consumed_at: Mapped[datetime | None] = ts(nullable=True)


# ---------------------------------------------------------------- workspaces
class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[UUID] = pk()
    name: Mapped[str] = mapped_column(String(80))
    slug: Mapped[str] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    created_at: Mapped[datetime] = ts(default_now=True)
    updated_at: Mapped[datetime] = ts(default_now=True)
    deleted_at: Mapped[datetime | None] = ts(nullable=True)


class Membership(Base):
    __tablename__ = "memberships"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref("workspaces.id")
    user_id: Mapped[UUID] = ref("users.id")
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = ts(default_now=True)
    updated_at: Mapped[datetime] = ts(default_now=True)


class Invitation(Base):
    __tablename__ = "invitations"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref("workspaces.id")
    email_normalized: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    invited_by: Mapped[UUID] = ref("users.id")
    created_at: Mapped[datetime] = ts(default_now=True)
    expires_at: Mapped[datetime] = ts()
    accepted_at: Mapped[datetime | None] = ts(nullable=True)
    accepted_by: Mapped[UUID | None] = ref("users.id", nullable=True)
    revoked_at: Mapped[datetime | None] = ts(nullable=True)


# ---------------------------------------------------------------- GitHub
class RepositoryConnection(Base):
    __tablename__ = "repository_connections"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref("workspaces.id")
    installation_id: Mapped[int] = mapped_column(BigInteger)
    github_account_id: Mapped[int] = mapped_column(BigInteger)
    github_account_login: Mapped[str] = mapped_column(Text)
    connected_by: Mapped[UUID] = ref("users.id")
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    last_verified_at: Mapped[datetime] = ts(default_now=True)
    created_at: Mapped[datetime] = ts(default_now=True)
    updated_at: Mapped[datetime] = ts(default_now=True)


class Repository(Base):
    __tablename__ = "repositories"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    connection_id: Mapped[UUID] = ref()
    provider_repo_id: Mapped[int] = mapped_column(BigInteger)
    owner: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(Text)
    default_branch: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = ts(default_now=True)
    updated_at: Mapped[datetime] = ts(default_now=True)
    access_revoked_at: Mapped[datetime | None] = ts(nullable=True)  # outside the connector's grant (0004)


# ---------------------------------------------------------------- projects and snapshots
class Project(Base):
    __tablename__ = "projects"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref("workspaces.id")
    repository_id: Mapped[UUID] = ref()
    name: Mapped[str] = mapped_column(String(80))
    slug: Mapped[str] = mapped_column(String(48))
    tracked_ref: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = jsonb(default="'{}'::jsonb")
    config_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = ts(default_now=True)
    updated_at: Mapped[datetime] = ts(default_now=True)
    deleted_at: Mapped[datetime | None] = ts(nullable=True)


class Snapshot(Base):
    __tablename__ = "snapshots"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    project_id: Mapped[UUID] = ref()
    commit_sha: Mapped[str] = mapped_column(Text)
    observed_ref: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    rejection_code: Mapped[str | None] = mapped_column(Text)
    archive_key: Mapped[str | None] = mapped_column(Text)
    digest: Mapped[str | None] = mapped_column(Text)
    source_bytes: Mapped[int | None] = mapped_column(BigInteger)
    file_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = ts(default_now=True)
    updated_at: Mapped[datetime] = ts(default_now=True)


class SourceFile(Base):
    __tablename__ = "source_files"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    snapshot_id: Mapped[UUID] = ref()
    path: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(Text)
    blob_key: Mapped[str | None] = mapped_column(Text)
    line_count: Mapped[int | None] = mapped_column(Integer)
    byte_count: Mapped[int] = mapped_column(BigInteger)
    scan_status: Mapped[str] = mapped_column(Text)
    scan_reason: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------- estimates, builds, tasks, events
class Estimate(Base):
    __tablename__ = "estimates"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    project_id: Mapped[UUID] = ref()
    snapshot_id: Mapped[UUID] = ref()
    config: Mapped[dict[str, Any]] = jsonb()
    config_digest: Mapped[str] = mapped_column(Text)
    price_version: Mapped[str] = mapped_column(Text)
    low_micro_usd: Mapped[int] = mapped_column(BigInteger)
    high_micro_usd: Mapped[int] = mapped_column(BigInteger)
    token_estimate: Mapped[dict[str, Any]] = jsonb(default="'{}'::jsonb")
    created_at: Mapped[datetime] = ts(default_now=True)
    expires_at: Mapped[datetime] = ts()


class Build(Base):
    __tablename__ = "builds"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    project_id: Mapped[UUID] = ref()
    snapshot_id: Mapped[UUID] = ref()
    estimate_id: Mapped[UUID | None] = ref(nullable=True)
    retry_of_id: Mapped[UUID | None] = ref(nullable=True)
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    config: Mapped[dict[str, Any]] = jsonb()
    config_digest: Mapped[str] = mapped_column(Text)
    engine_version: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    cap_micro_usd: Mapped[int] = mapped_column(BigInteger)
    reserved_micro_usd: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    settled_micro_usd: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    cancel_requested_at: Mapped[datetime | None] = ts(nullable=True)
    manifest_key: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID] = ref("users.id")
    created_at: Mapped[datetime] = ts(default_now=True)
    updated_at: Mapped[datetime] = ts(default_now=True)
    completed_at: Mapped[datetime | None] = ts(nullable=True)


class BuildTask(Base):
    __tablename__ = "build_tasks"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    build_id: Mapped[UUID] = ref()
    stage: Mapped[str] = mapped_column(Text)
    variant: Mapped[str] = mapped_column(Text, server_default=text("'en-standard'"))
    work_key: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    lease_generation: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    lease_until: Mapped[datetime | None] = ts(nullable=True)
    heartbeat_at: Mapped[datetime | None] = ts(nullable=True)
    next_attempt_at: Mapped[datetime | None] = ts(nullable=True)
    output_key: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = ts(default_now=True)
    updated_at: Mapped[datetime] = ts(default_now=True)
    notified_at: Mapped[datetime | None] = ts(nullable=True)  # last queue notification (0005)


class BuildEvent(Base):
    __tablename__ = "build_events"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    build_id: Mapped[UUID] = ref()
    sequence: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = jsonb(default="'{}'::jsonb")
    created_at: Mapped[datetime] = ts(default_now=True)


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    build_id: Mapped[UUID] = ref()
    kind: Mapped[str] = mapped_column(Text)
    variant: Mapped[str] = mapped_column(Text, server_default=text("'en-standard'"))
    object_key: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text)
    byte_count: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'stored'"))
    expires_at: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts(default_now=True)


class OutboxEvent(Base):
    __tablename__ = "outbox"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID | None] = ref("workspaces.id", nullable=True)
    aggregate_id: Mapped[UUID] = ref()
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = jsonb()
    available_at: Mapped[datetime] = ts(default_now=True)
    published_at: Mapped[datetime | None] = ts(nullable=True)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = ts(default_now=True)


# ---------------------------------------------------------------- usage ledger
class UsageAccount(Base):
    __tablename__ = "usage_accounts"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref("workspaces.id")
    period_start: Mapped[datetime] = ts()
    period_end: Mapped[datetime] = ts()
    allowance_micro_usd: Mapped[int] = mapped_column(BigInteger)
    reserved_micro_usd: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    settled_micro_usd: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))


class UsageEntry(Base):
    """Append-only: the runtime role cannot UPDATE or DELETE."""

    __tablename__ = "usage_entries"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    account_id: Mapped[UUID] = ref()
    build_id: Mapped[UUID | None] = ref(nullable=True)
    kind: Mapped[str] = mapped_column(Text)
    amount_micro_usd: Mapped[int] = mapped_column(BigInteger)
    idempotency_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = ts(default_now=True)


# ---------------------------------------------------------------- audit and idempotency
class AuditEvent(Base):
    """Append-only: the runtime role cannot UPDATE or DELETE."""

    __tablename__ = "audit_events"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID | None] = ref("workspaces.id", nullable=True)
    actor_user_id: Mapped[UUID | None] = ref("users.id", nullable=True)
    action: Mapped[str] = mapped_column(Text)
    resource_type: Mapped[str] = mapped_column(Text)
    resource_id: Mapped[UUID | None] = ref(nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = ts(default_now=True)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    id: Mapped[UUID] = pk()
    user_id: Mapped[UUID] = ref("users.id")
    workspace_id: Mapped[UUID | None] = ref("workspaces.id", nullable=True)
    route: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text)
    request_digest: Mapped[str] = mapped_column(Text)
    response_status: Mapped[int | None] = mapped_column(SmallInteger)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = ts(default_now=True)
    expires_at: Mapped[datetime] = ts()


# ---------------------------------------------------------------- engine output and webhooks (0005)
class Symbol(Base):
    __tablename__ = "symbols"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    build_id: Mapped[UUID] = ref()
    source_file_id: Mapped[UUID] = ref()
    symbol_key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    parent_key: Mapped[str | None] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    start_column: Mapped[int] = mapped_column(Integer)
    end_column: Mapped[int] = mapped_column(Integer)
    signature: Mapped[str | None] = mapped_column(Text)
    rank: Mapped[float] = mapped_column(Double, server_default=text("0"))
    card: Mapped[dict[str, Any]] = jsonb(default="'{}'::jsonb")


class ReferenceEdge(Base):
    __tablename__ = "reference_edges"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    build_id: Mapped[UUID] = ref()
    from_symbol_id: Mapped[UUID | None] = ref(nullable=True)
    to_symbol_id: Mapped[UUID] = ref()
    source_file_id: Mapped[UUID] = ref()
    line: Mapped[int] = mapped_column(Integer)
    column: Mapped[int] = mapped_column("column", Integer)
    resolution: Mapped[str] = mapped_column(Text)
    ambiguity_group: Mapped[str | None] = mapped_column(Text)


class WebhookReceipt(Base):
    __tablename__ = "webhook_receipts"
    id: Mapped[UUID] = pk()
    provider: Mapped[str] = mapped_column(Text)
    provider_event_id: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text)
    payload_digest: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default=text("'received'"))
    received_at: Mapped[datetime] = ts(default_now=True)
    processed_at: Mapped[datetime | None] = ts(nullable=True)
