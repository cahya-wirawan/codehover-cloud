"""The only place object keys are built: tenant data lives under the workspace prefix (04); the only
other prefix is the tombstone log, which holds identifiers and no content (05 "deletion_requests")."""

from __future__ import annotations

from uuid import UUID


def blob_key(workspace_id: UUID, project_id: UUID, snapshot_id: UUID, sha256: str) -> str:
    return f"workspaces/{workspace_id}/projects/{project_id}/snapshots/{snapshot_id}/blobs/{sha256}"


def build_key(workspace_id: UUID, project_id: UUID, build_id: UUID, name: str) -> str:
    return f"workspaces/{workspace_id}/projects/{project_id}/builds/{build_id}/{name}"


def project_prefix(workspace_id: UUID, project_id: UUID) -> str:
    return f"workspaces/{workspace_id}/projects/{project_id}/"


def build_prefix(workspace_id: UUID, project_id: UUID, build_id: UUID) -> str:
    return f"workspaces/{workspace_id}/projects/{project_id}/builds/{build_id}/"


def snapshot_prefix(workspace_id: UUID, project_id: UUID, snapshot_id: UUID) -> str:
    return f"workspaces/{workspace_id}/projects/{project_id}/snapshots/{snapshot_id}/"


def workspace_prefix(workspace_id: UUID) -> str:
    return f"workspaces/{workspace_id}/"


TOMBSTONE_PREFIX = "tombstones/"


def tombstone_key(request_id: UUID) -> str:
    return f"{TOMBSTONE_PREFIX}{request_id}.json"
