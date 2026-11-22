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
    Boolean,
    Computed,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
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
    namespace_id: Mapped[UUID | None] = ref("namespaces.id", nullable=True)  # 0038: its owner


class Membership(Base):
    __tablename__ = "memberships"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref("workspaces.id")
    user_id: Mapped[UUID] = ref("users.id")
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = ts(default_now=True)
    updated_at: Mapped[datetime] = ts(default_now=True)
    via_organization: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))  # 0038
    direct_role: Mapped[str | None] = mapped_column(Text)


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
    provider: Mapped[str] = mapped_column(Text, server_default=text("'github'"))  # github | upload (0025)
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    github_account_id: Mapped[int | None] = mapped_column(BigInteger)
    github_account_login: Mapped[str | None] = mapped_column(Text)
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
    provider_repo_id: Mapped[int | None] = mapped_column(BigInteger)  # None for uploads (0025)
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
    last_observed_sha: Mapped[str | None] = mapped_column(Text)  # 0014
    last_observed_at: Mapped[datetime | None] = ts(nullable=True)
    push_policy: Mapped[str] = mapped_column(Text, server_default=text("'off'"))
    push_cap_micro_usd: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    push_policy_by: Mapped[UUID | None] = ref("users.id", nullable=True)
    push_policy_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    access: Mapped[str] = mapped_column(Text, server_default=text("'workspace'"))  # 0027
    access_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    namespace_id: Mapped[UUID | None] = ref(
        "namespaces.id", nullable=True
    )  # 0039: the owner, from the workspace


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
    content_purged_at: Mapped[datetime | None] = ts(nullable=True)


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
    created_by: Mapped[UUID | None] = ref("users.id", nullable=True)  # 0006


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
    content_purged_at: Mapped[datetime | None] = ts(nullable=True)
    usage_account_id: Mapped[UUID | None] = ref(nullable=True)  # reservation account (0006)


class BuildTask(Base):
    """Owned by exactly one build or one snapshot (snapshot acquisition, 0006)."""

    __tablename__ = "build_tasks"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    build_id: Mapped[UUID | None] = ref(nullable=True)
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
    snapshot_id: Mapped[UUID | None] = ref(nullable=True)  # 0006


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
    attempt_id: Mapped[UUID | None] = ref(nullable=True)  # 0006


class ProviderAttempt(Base):
    """One outbound model request: earmark before, settle once after (0006)."""

    __tablename__ = "provider_attempts"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    build_id: Mapped[UUID] = ref()
    task_id: Mapped[UUID] = ref()
    attempt_number: Mapped[int] = mapped_column(Integer)
    lease_generation: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    model_id: Mapped[str] = mapped_column(Text)
    request_digest: Mapped[str] = mapped_column(Text)
    provider_request_id: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default=text("'started'"))
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_read_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger)
    price_snapshot: Mapped[dict[str, Any]] = jsonb()
    max_reserved_micro_usd: Mapped[int] = mapped_column(BigInteger)
    settled_micro_usd: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(Text)
    output_digest: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = ts(default_now=True)
    completed_at: Mapped[datetime | None] = ts(nullable=True)


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
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


# ---------------------------------------------------------------- generation (0007)
class GenerationCache(Base):
    """Tenant-local validated model output; also the fenced checkpoint of generation stages."""

    __tablename__ = "generation_cache"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref("workspaces.id")
    cache_key: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    output: Mapped[dict[str, Any]] = jsonb()
    output_digest: Mapped[str] = mapped_column(Text)
    model_id: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    attempt_id: Mapped[UUID | None] = ref(nullable=True)
    created_at: Mapped[datetime] = ts(default_now=True)
    expires_at: Mapped[datetime] = ts(default_now=False)


class FileExplanation(Base):
    __tablename__ = "file_explanations"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    build_id: Mapped[UUID] = ref()
    source_file_id: Mapped[UUID] = ref()
    status: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, server_default=text("''"))
    chunks: Mapped[list[Any]] = jsonb(default="'[]'::jsonb")
    provenance: Mapped[dict[str, Any]] = jsonb(default="'{}'::jsonb")


class GuidePage(Base):
    __tablename__ = "guide_pages"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    build_id: Mapped[UUID] = ref()
    slug: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    section: Mapped[str] = mapped_column(Text, server_default=text("''"))  # 0031
    ordinal: Mapped[int] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(Text, server_default=text("''"))
    source_ranges: Mapped[list[Any]] = jsonb()
    status: Mapped[str] = mapped_column(Text, server_default=text("'planned'"))
    markdown_key: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[dict[str, Any]] = jsonb(default="'{}'::jsonb")
    original_markdown_key: Mapped[str | None] = mapped_column(Text)  # 0008
    check_status: Mapped[str] = mapped_column(Text, server_default=text("'unchecked'"))
    created_at: Mapped[datetime] = ts(default_now=True)


class ClaimCheck(Base):
    """One extracted claim and cited range with its deterministic and checker results (0008)."""

    __tablename__ = "claim_checks"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    build_id: Mapped[UUID] = ref()
    guide_page_id: Mapped[UUID] = ref()
    claim_key: Mapped[str] = mapped_column(Text)
    claim_text: Mapped[str] = mapped_column(Text)
    ordinal: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    current: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    citation: Mapped[str] = mapped_column(Text)
    source_file_id: Mapped[UUID | None] = ref(nullable=True)
    path: Mapped[str | None] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    source_hash: Mapped[str | None] = mapped_column(Text)
    deterministic: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, server_default=text("''"))
    evidence_lines: Mapped[list[Any]] = jsonb(default="'[]'::jsonb")
    checker_model: Mapped[str | None] = mapped_column(Text)
    waived_by: Mapped[UUID | None] = ref("users.id", nullable=True)
    waived_reason: Mapped[str | None] = mapped_column(Text)
    waived_at: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts(default_now=True)
    checked_at: Mapped[datetime | None] = ts(nullable=True)


class SearchDocument(Base):
    """Build-scoped derived search row (0009); `name_lower` and `document` are generated columns."""

    __tablename__ = "search_documents"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    build_id: Mapped[UUID] = ref()
    kind: Mapped[str] = mapped_column(Text)
    ref_id: Mapped[UUID] = ref()
    name: Mapped[str] = mapped_column(Text)
    name_lower: Mapped[str | None] = mapped_column(Text, Computed("lower(name)"))
    path: Mapped[str | None] = mapped_column(Text)
    line: Mapped[int | None] = mapped_column(Integer)
    symbol_kind: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str] = mapped_column(Text, server_default=text("''"))
    terms: Mapped[str] = mapped_column(Text, server_default=text("''"))
    body: Mapped[str] = mapped_column(Text, server_default=text("''"))
    document: Mapped[Any] = mapped_column(TSVECTOR, Computed("document"), nullable=True)


class Publication(Base):
    """The mutable pointer from a project to one ready build (0010)."""

    __tablename__ = "publications"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    project_id: Mapped[UUID] = ref()
    build_id: Mapped[UUID] = ref()
    visibility: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    acknowledged_partial: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    suspended_at: Mapped[datetime | None] = ts(nullable=True)
    suspended_reason: Mapped[str | None] = mapped_column(Text)
    license_spdx: Mapped[str | None] = mapped_column(Text)  # 0029: public sites of public-source repositories
    license_path: Mapped[str | None] = mapped_column(Text)
    variant_build_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), server_default=text("'{}'")
    )  # 0033
    published_by: Mapped[UUID] = ref("users.id")
    created_at: Mapped[datetime] = ts(default_now=True)
    updated_at: Mapped[datetime] = ts(default_now=True)


class Export(Base):
    """A downloadable package of one ready build (0011)."""

    __tablename__ = "exports"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    build_id: Mapped[UUID] = ref()
    format: Mapped[str] = mapped_column(Text)
    variant: Mapped[str] = mapped_column(Text, server_default=text("'en-standard'"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    requested_by: Mapped[UUID] = ref("users.id")
    object_key: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(Text)
    byte_count: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = ts(default_now=True)
    completed_at: Mapped[datetime | None] = ts(nullable=True)
    expires_at: Mapped[datetime | None] = ts(nullable=True)


class DeletionRequest(Base):
    """A deletion tombstone: identifiers only; drives the purge (0015)."""

    __tablename__ = "deletion_requests"
    id: Mapped[UUID] = pk()
    scope: Mapped[str] = mapped_column(Text)
    workspace_id: Mapped[UUID | None] = ref(nullable=True)
    project_id: Mapped[UUID | None] = ref(nullable=True)
    user_id: Mapped[UUID | None] = ref(nullable=True)
    requested_by: Mapped[UUID | None] = ref(nullable=True)
    state: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now() + '00:05:00'::interval")
    )
    lease_until: Mapped[datetime | None] = ts(nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error_code: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = ts(default_now=True)
    completed_at: Mapped[datetime | None] = ts(nullable=True)


class Operator(Base):
    """Platform operations role, granted by the deployment (0018)."""

    __tablename__ = "operators"
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    granted_at: Mapped[datetime] = ts(default_now=True)
    note: Mapped[str] = mapped_column(Text, server_default=text("''"))
    revoked_at: Mapped[datetime | None] = ts(nullable=True)


class OperatorEvent(Base):
    __tablename__ = "operator_events"
    id: Mapped[UUID] = pk()
    operator_user_id: Mapped[UUID] = ref("users.id")
    action: Mapped[str] = mapped_column(Text)
    workspace_id: Mapped[UUID | None] = ref(nullable=True)
    target_id: Mapped[UUID | None] = ref(nullable=True)
    reason: Mapped[str] = mapped_column(Text, server_default=text("''"))
    created_at: Mapped[datetime] = ts(default_now=True)


class PlatformSettings(Base):
    __tablename__ = "platform_settings"
    id: Mapped[bool] = mapped_column(Boolean, primary_key=True, server_default=text("true"))
    builds_paused: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    paused_reason: Mapped[str] = mapped_column(Text, server_default=text("''"))
    updated_at: Mapped[datetime] = ts(default_now=True)


class EntitlementGrant(Base):
    __tablename__ = "entitlement_grants"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref("workspaces.id")
    allowance_micro_usd: Mapped[int] = mapped_column(BigInteger)
    starts_at: Mapped[datetime] = ts(default_now=True)
    expires_at: Mapped[datetime] = ts()
    reason: Mapped[str] = mapped_column(Text)
    granted_by: Mapped[UUID] = ref("users.id")
    created_at: Mapped[datetime] = ts(default_now=True)
    revoked_at: Mapped[datetime | None] = ts(nullable=True)
    kind: Mapped[str] = mapped_column(Text, server_default=text("'manual'"))
    once: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class SupportGrant(Base):
    __tablename__ = "support_grants"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref("workspaces.id")
    granted_by: Mapped[UUID] = ref("users.id")
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = ts(default_now=True)
    expires_at: Mapped[datetime] = ts()
    revoked_at: Mapped[datetime | None] = ts(nullable=True)


class RateLimit(Base):
    """A fixed-window counter shared by all replicas; the bucket is an HMAC (0019)."""

    __tablename__ = "rate_limits"
    bucket: Mapped[str] = mapped_column(Text, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer)


# ---------------------------------------------------------------- billing (0022)
class BillingCustomer(Base):
    __tablename__ = "billing_customers"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref("workspaces.id")
    provider: Mapped[str] = mapped_column(Text, server_default=text("'stripe'"))
    provider_customer_id: Mapped[str] = mapped_column(Text)
    disputed_at: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts(default_now=True)
    open_disputes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    dispute_lost_at: Mapped[datetime | None] = ts(nullable=True)


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref("workspaces.id")
    provider_subscription_id: Mapped[str] = mapped_column(Text)
    provider_customer_id: Mapped[str] = mapped_column(Text)
    plan_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    current_period_start: Mapped[datetime | None] = ts(nullable=True)
    current_period_end: Mapped[datetime | None] = ts(nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    ended_at: Mapped[datetime | None] = ts(nullable=True)
    past_due_since: Mapped[datetime | None] = ts(nullable=True)
    synced_at: Mapped[datetime] = ts(default_now=True)
    created_at: Mapped[datetime] = ts(default_now=True)
    updated_at: Mapped[datetime] = ts(default_now=True)


# ---------------------------------------------------------------- push rebuilds (0024)
class PushRequest(Base):
    __tablename__ = "push_requests"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref("workspaces.id")
    project_id: Mapped[UUID] = ref()
    snapshot_id: Mapped[UUID | None] = ref(nullable=True)
    build_id: Mapped[UUID | None] = ref(nullable=True)
    commit_sha: Mapped[str] = mapped_column(Text)
    ref: Mapped[str] = mapped_column(Text)  # the branch; defined after the ref() columns it would shadow
    pushes: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    first_received_at: Mapped[datetime] = ts(default_now=True)
    due_at: Mapped[datetime] = ts()
    state: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    outcome: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = ts(default_now=True)


# ---------------------------------------------------------------- own keys and batches (0026)
class ProviderKey(Base):
    __tablename__ = "provider_keys"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref("workspaces.id")
    provider: Mapped[str] = mapped_column(Text)
    sealed_key: Mapped[bytes | None] = mapped_column(LargeBinary)
    hint: Mapped[str] = mapped_column(Text)
    created_by: Mapped[UUID] = ref("users.id")
    created_at: Mapped[datetime] = ts(default_now=True)
    verified_at: Mapped[datetime | None] = ts(nullable=True)
    revoked_at: Mapped[datetime | None] = ts(nullable=True)


class ProviderBatch(Base):
    __tablename__ = "provider_batches"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    build_id: Mapped[UUID] = ref()
    task_id: Mapped[UUID] = ref()
    stage: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    provider_batch_id: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default=text("'submitting'"))
    items: Mapped[list[Any]] = jsonb()
    created_at: Mapped[datetime] = ts(default_now=True)
    completed_at: Mapped[datetime | None] = ts(nullable=True)


# ---------------------------------------------------------------- per-project access (0027)
class ProjectMember(Base):
    __tablename__ = "project_members"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref()
    project_id: Mapped[UUID] = ref()
    user_id: Mapped[UUID] = ref()
    role: Mapped[str] = mapped_column(Text)
    granted_by: Mapped[UUID] = ref("users.id")
    created_at: Mapped[datetime] = ts(default_now=True)


# ---------------------------------------------------------------- owner notices (0034)
class OwnerNotice(Base):
    __tablename__ = "owner_notices"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref("workspaces.id")
    kind: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = jsonb(default="'{}'::jsonb")
    state: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = ts(default_now=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    recipients: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = ts(default_now=True)
    sent_at: Mapped[datetime | None] = ts(nullable=True)


# ---------------------------------------------------------------- access tokens (0035)
class AccessToken(Base):
    __tablename__ = "access_tokens"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref("workspaces.id")
    user_id: Mapped[UUID] = ref("users.id")
    name: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    prefix: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text, server_default=text("'mcp:read'"))
    created_at: Mapped[datetime] = ts(default_now=True)
    expires_at: Mapped[datetime] = ts()
    last_used_at: Mapped[datetime | None] = ts(nullable=True)
    revoked_at: Mapped[datetime | None] = ts(nullable=True)
    grant_id: Mapped[UUID | None] = ref(nullable=True)  # 0037: issued through MCP sign-in
    project_id: Mapped[UUID | None] = ref(nullable=True)


# ---------------------------------------------------------------- site aliases (0039)
class SiteAlias(Base):
    """A pre-0039 one-segment site address, leading to its project's /sites/<owner>/<project>."""

    __tablename__ = "site_aliases"
    alias: Mapped[str] = mapped_column(Text, primary_key=True)
    project_id: Mapped[UUID] = ref("projects.id")
    created_at: Mapped[datetime] = ts(default_now=True)
    expires_at: Mapped[datetime] = ts(default_now=True)


# ---------------------------------------------------------------- MCP sign-in (0037)
class OAuthClient(Base):
    __tablename__ = "oauth_clients"
    id: Mapped[UUID] = pk()
    client_id: Mapped[str] = mapped_column(Text)
    client_name: Mapped[str] = mapped_column(Text)
    redirect_uris: Mapped[list[str]] = mapped_column(ARRAY(Text))
    created_at: Mapped[datetime] = ts(default_now=True)
    last_used_at: Mapped[datetime | None] = ts(nullable=True)


class OAuthAuthorization(Base):
    __tablename__ = "oauth_authorizations"
    id: Mapped[UUID] = pk()
    client: Mapped[UUID] = ref("oauth_clients.id")
    redirect_uri: Mapped[str] = mapped_column(Text)
    code_challenge: Mapped[str] = mapped_column(Text)
    resource: Mapped[str] = mapped_column(Text)
    site: Mapped[str] = mapped_column(Text)  # 0039: "<owner>/<project>"
    state: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = ts(default_now=True)
    expires_at: Mapped[datetime] = ts()
    decided_at: Mapped[datetime | None] = ts(nullable=True)
    approved: Mapped[bool | None] = mapped_column(Boolean)
    user_id: Mapped[UUID | None] = ref("users.id", nullable=True)
    workspace_id: Mapped[UUID | None] = ref(nullable=True)
    project_id: Mapped[UUID | None] = ref(nullable=True)
    code_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    code_expires_at: Mapped[datetime | None] = ts(nullable=True)
    redeemed_at: Mapped[datetime | None] = ts(nullable=True)


class OAuthGrant(Base):
    __tablename__ = "oauth_grants"
    id: Mapped[UUID] = pk()
    workspace_id: Mapped[UUID] = ref("workspaces.id")
    user_id: Mapped[UUID] = ref("users.id")
    project_id: Mapped[UUID] = ref()
    client: Mapped[UUID] = ref("oauth_clients.id")
    client_name: Mapped[str] = mapped_column(Text)
    authorization_id: Mapped[UUID] = ref()
    refresh_hash: Mapped[bytes] = mapped_column(LargeBinary)
    previous_refresh_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    refresh_expires_at: Mapped[datetime] = ts()
    created_at: Mapped[datetime] = ts(default_now=True)
    last_used_at: Mapped[datetime | None] = ts(nullable=True)
    revoked_at: Mapped[datetime | None] = ts(nullable=True)


# ---------------------------------------------------------------- namespaces and organizations (0038)
class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[UUID] = pk()
    name: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    created_at: Mapped[datetime] = ts(default_now=True)
    updated_at: Mapped[datetime] = ts(default_now=True)
    deleted_at: Mapped[datetime | None] = ts(nullable=True)


class OrganizationMember(Base):
    __tablename__ = "organization_members"
    id: Mapped[UUID] = pk()
    organization_id: Mapped[UUID] = ref("organizations.id")
    user_id: Mapped[UUID] = ref("users.id")
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = ts(default_now=True)
    updated_at: Mapped[datetime] = ts(default_now=True)


class OrganizationInvitation(Base):
    __tablename__ = "organization_invitations"
    id: Mapped[UUID] = pk()
    organization_id: Mapped[UUID] = ref("organizations.id")
    user_id: Mapped[UUID] = ref("users.id")
    role: Mapped[str] = mapped_column(Text)
    invited_by: Mapped[UUID] = ref("users.id")
    created_at: Mapped[datetime] = ts(default_now=True)
    expires_at: Mapped[datetime] = ts(default_now=True)
    accepted_at: Mapped[datetime | None] = ts(nullable=True)
    declined_at: Mapped[datetime | None] = ts(nullable=True)
    revoked_at: Mapped[datetime | None] = ts(nullable=True)


class Namespace(Base):
    __tablename__ = "namespaces"
    id: Mapped[UUID] = pk()
    handle: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    user_id: Mapped[UUID | None] = ref("users.id", nullable=True)
    organization_id: Mapped[UUID | None] = ref("organizations.id", nullable=True)
    confirmed_at: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts(default_now=True)
    updated_at: Mapped[datetime] = ts(default_now=True)


class HandleReservation(Base):
    __tablename__ = "handle_reservations"
    handle: Mapped[str] = mapped_column(Text, primary_key=True)
    namespace_id: Mapped[UUID | None] = ref("namespaces.id", nullable=True)
    released_at: Mapped[datetime] = ts()
    reserved_until: Mapped[datetime] = ts()
