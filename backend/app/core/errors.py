"""The single error envelope (06_API_SPEC.md "Shared behavior").

`{"error": {"code", "message", "request_id", "retryable", "details"}}` for every error response.
Never include source, credentials, stack traces or provider bodies.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.request_context import current_request_id

logger = logging.getLogger(__name__)

_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    410: "GONE",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "VALIDATION_FAILED",
    429: "RATE_LIMITED",
    503: "UNAVAILABLE",
}


class ApiError(Exception):
    """Raise from handlers/services for an expected, client-facing failure."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        self.headers = headers


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = {
        "error": {
            "code": code,
            "message": message,
            "request_id": current_request_id(),
            "retryable": retryable,
            "details": details or {},
        }
    }
    return JSONResponse(status_code=status_code, content=body, headers=headers)


async def _api_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return error_response(
        exc.status_code,
        exc.code,
        exc.message,
        retryable=exc.retryable,
        details=exc.details,
        headers=exc.headers,
    )


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _STATUS_CODES.get(exc.status_code, "ERROR")
    message = (
        HTTPStatus(exc.status_code).phrase if exc.status_code in HTTPStatus._value2member_map_ else "Error"
    )
    headers = dict(exc.headers) if exc.headers else None
    return error_response(
        exc.status_code, code, message, retryable=exc.status_code in (429, 503), headers=headers
    )


async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # Field locations and error types only; never echo submitted values back.
    fields = [
        {"location": [str(part) for part in err.get("loc", ())], "type": err.get("type", "invalid")}
        for err in exc.errors()
    ]
    return error_response(
        422, "VALIDATION_FAILED", "The request is invalid.", details={"fields": fields[:50]}
    )


async def _unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled exception", exc_info=exc)
    return error_response(500, "INTERNAL", "An unexpected error occurred.", retryable=True)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unexpected_error)
