"""Object storage (04 "Snapshot and artifact contracts"): private, keyed under the workspace prefix.

The S3 adapter runs boto3 in a worker thread (bounded by the default executor). Keys are produced only
by `app.engine.keys` so every object lives under `workspaces/{wid}/...` (also enforced for artifact rows
by a CHECK constraint).
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Protocol


class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> str: ...  # returns sha256 hex
    async def get(self, key: str) -> bytes: ...
    async def exists(self, key: str) -> bool: ...


class MemoryObjectStore:
    """In-process store for tests and local runs without S3."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        self.objects[key] = (data, content_type)
        return hashlib.sha256(data).hexdigest()

    async def get(self, key: str) -> bytes:
        return self.objects[key][0]

    async def exists(self, key: str) -> bool:
        return key in self.objects


class S3ObjectStore:
    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        digest = hashlib.sha256(data).hexdigest()
        await asyncio.to_thread(
            self._client.put_object, Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
        )
        return digest

    async def get(self, key: str) -> bytes:
        response = await asyncio.to_thread(self._client.get_object, Bucket=self._bucket, Key=key)
        body: bytes = await asyncio.to_thread(response["Body"].read)
        return body

    async def exists(self, key: str) -> bool:
        try:
            await asyncio.to_thread(self._client.head_object, Bucket=self._bucket, Key=key)
        except Exception:  # botocore ClientError 404 and friends
            return False
        return True
