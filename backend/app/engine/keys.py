"""The only place object keys are built: everything lives under the workspace prefix (04)."""

from __future__ import annotations

from uuid import UUID


def blob_key(workspace_id: UUID, project_id: UUID, snapshot_id: UUID, sha256: str) -> str:
    return f"workspaces/{workspace_id}/projects/{project_id}/snapshots/{snapshot_id}/blobs/{sha256}"


def build_key(workspace_id: UUID, project_id: UUID, build_id: UUID, name: str) -> str:
    return f"workspaces/{workspace_id}/projects/{project_id}/builds/{build_id}/{name}"
