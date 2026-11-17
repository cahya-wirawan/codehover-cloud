"""Request ID propagation: accept a well-formed upstream ID (nginx `$request_id`), otherwise mint one."""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "x-request-id"
_VALID = re.compile(r"^[A-Za-z0-9-]{8,64}$")

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def current_request_id() -> str | None:
    return request_id_var.get()


def _choose_request_id(raw: bytes | None) -> str:
    if raw is not None:
        candidate = raw.decode("latin-1")
        if _VALID.fullmatch(candidate):
            return candidate
    return uuid.uuid4().hex


class RequestIdMiddleware:
    """Pure ASGI middleware so the ID is set before routing and visible to every handler and log line."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = next((v for k, v in scope["headers"] if k == REQUEST_ID_HEADER.encode()), None)
        request_id = _choose_request_id(incoming)
        token = request_id_var.set(request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k != REQUEST_ID_HEADER.encode()]
                headers.append((REQUEST_ID_HEADER.encode(), request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)
