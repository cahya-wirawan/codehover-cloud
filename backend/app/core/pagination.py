"""Cursor pagination (06 "Shared behavior").

Responses are `{items: [...], next_cursor: "opaque-or-null"}`, default limit 25, max 100. A cursor
encodes the keyset position `(created_at, id)` plus a digest of the route/filters it belongs to, and is
HMAC-signed so clients cannot forge positions or reuse a cursor with different filters.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.errors import ApiError

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


@dataclass(frozen=True)
class Position:
    created_at: datetime
    id: UUID


def _key(secret: str) -> bytes:
    return hashlib.sha256(b"codehover/cursor:" + secret.encode()).digest()


def _scope_digest(scope: str) -> str:
    return hashlib.sha256(scope.encode()).hexdigest()[:16]


def encode_cursor(secret: str, scope: str, position: Position) -> str:
    body = json.dumps(
        {"t": position.created_at.isoformat(), "i": str(position.id), "s": _scope_digest(scope)},
        separators=(",", ":"),
    ).encode()
    mac = hmac.new(_key(secret), body, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(body + mac).rstrip(b"=").decode()


def decode_cursor(secret: str, scope: str, cursor: str | None) -> Position | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        body, mac = raw[:-16], raw[-16:]
        if not hmac.compare_digest(mac, hmac.new(_key(secret), body, hashlib.sha256).digest()[:16]):
            raise ValueError("signature")
        data = json.loads(body)
        if data["s"] != _scope_digest(scope):
            raise ValueError("scope")
        return Position(datetime.fromisoformat(data["t"]), UUID(data["i"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(400, "INVALID_CURSOR", "The pagination cursor is invalid.") from exc


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


def _sign(secret: str, data: dict[str, str]) -> str:
    body = json.dumps(data, separators=(",", ":")).encode()
    mac = hmac.new(_key(secret), body, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(body + mac).rstrip(b"=").decode()


def _verify(secret: str, cursor: str) -> dict[str, str]:
    raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    body, mac = raw[:-16], raw[-16:]
    if not hmac.compare_digest(mac, hmac.new(_key(secret), body, hashlib.sha256).digest()[:16]):
        raise ValueError("signature")
    data: dict[str, str] = json.loads(body)
    return data


def encode_path_cursor(secret: str, scope: str, path: str) -> str:
    """Keyset cursor over a unique text key (file paths in a snapshot), signed and scope-bound."""
    return _sign(secret, {"p": path, "s": _scope_digest(scope)})


def decode_path_cursor(secret: str, scope: str, cursor: str | None) -> str:
    """The last path of the previous page, or "" for the first page."""
    if cursor is None:
        return ""
    try:
        data = _verify(secret, cursor)
        if data["s"] != _scope_digest(scope) or not isinstance(data["p"], str):
            raise ValueError("scope")
        return data["p"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(400, "INVALID_CURSOR", "The pagination cursor is invalid.") from exc
