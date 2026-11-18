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
    async def list(self, prefix: str) -> list[str]: ...
    async def delete(self, key: str) -> None: ...  # idempotent: a missing object is not an error
    async def delete_prefix(self, prefix: str) -> int: ...  # returns the number of objects deleted


def _checked_prefix(prefix: str) -> str:
    """Bulk operations only ever address one tenant subtree or the tombstone log, never the bucket root."""
    if not (prefix.startswith("workspaces/") or prefix.startswith("tombstones/")) or not prefix.endswith("/"):
        raise ValueError("refusing an unscoped object prefix")
    if prefix.count("/") < 2 and not prefix.startswith("tombstones/"):
        raise ValueError("refusing an unscoped object prefix")
    return prefix


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

    async def list(self, prefix: str) -> list[str]:
        prefix = _checked_prefix(prefix)
        return sorted(k for k in self.objects if k.startswith(prefix))

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def delete_prefix(self, prefix: str) -> int:
        doomed = await self.list(prefix)
        for key in doomed:
            del self.objects[key]
        return len(doomed)


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

    async def list(self, prefix: str) -> list[str]:
        prefix = _checked_prefix(prefix)
        keys: list[str] = []
        token: str | None = None
        while True:
            extra = {"ContinuationToken": token} if token else {}
            page = await asyncio.to_thread(
                self._client.list_objects_v2, Bucket=self._bucket, Prefix=prefix, **extra
            )
            keys.extend(item["Key"] for item in page.get("Contents", []))
            if not page.get("IsTruncated"):
                return keys
            token = page["NextContinuationToken"]

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)

    async def delete_prefix(self, prefix: str) -> int:
        keys = await self.list(prefix)
        for start in range(0, len(keys), 1000):  # the S3 batch limit
            batch = [{"Key": key} for key in keys[start : start + 1000]]
            await asyncio.to_thread(
                self._client.delete_objects, Bucket=self._bucket, Delete={"Objects": batch, "Quiet": True}
            )
        return len(keys)
