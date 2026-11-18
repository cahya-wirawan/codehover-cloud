"""GitHub App webhooks: HMAC-verified over the raw body, deduplicated by delivery ID, receipt persisted
before acknowledging (06). Installation removal or suspension takes effect immediately (09, FR-027)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.errors import ApiError
from app.core.settings import Settings
from app.modules.identity.sessions import Db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["webhooks"])
MAX_BODY_BYTES = 2 * 1024 * 1024
INSTALLATION_ACTIONS = {"deleted": "revoked", "suspend": "suspended", "unsuspend": "active"}


@router.post("/webhooks/github", status_code=202)
async def github_webhook(request: Request, db: Db) -> JSONResponse:
    settings: Settings = request.app.state.settings
    if settings.github_webhook_secret is None:
        raise ApiError(503, "WEBHOOKS_DISABLED", "Webhooks are not configured.")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", "Payload too large.")
    expected = (
        "sha256="
        + hmac.new(
            settings.github_webhook_secret.get_secret_value().encode(), body, hashlib.sha256
        ).hexdigest()
    )
    if not hmac.compare_digest(request.headers.get("x-hub-signature-256", ""), expected):
        raise ApiError(401, "INVALID_SIGNATURE", "Signature verification failed.")
    delivery = request.headers.get("x-github-delivery", "")
    event = request.headers.get("x-github-event", "")
    if not delivery or len(delivery) > 200:
        raise ApiError(400, "BAD_REQUEST", "Missing delivery ID.")
    receipt = (
        await db.execute(
            text(
                "INSERT INTO webhook_receipts(provider, provider_event_id, event_type, payload_digest)"
                " VALUES ('github', :d, :e, :h) ON CONFLICT (provider, provider_event_id) DO NOTHING RETURNING id"
            ),
            {"d": delivery, "e": event, "h": hashlib.sha256(body).hexdigest()},
        )
    ).scalar_one_or_none()
    if receipt is None:
        await db.commit()
        return JSONResponse({"duplicate": True}, status_code=202)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {}
    state, changed = "ignored", 0
    installation_id = (payload.get("installation") or {}).get("id") if isinstance(payload, dict) else None
    status = INSTALLATION_ACTIONS.get(payload.get("action", "")) if event == "installation" else None
    if status and isinstance(installation_id, int):
        changed = (
            await db.execute(
                text("SELECT app_private.set_installation_status(:i, :s)"),
                {"i": installation_id, "s": status},
            )
        ).scalar_one()
        state = "processed"
    await db.execute(
        text("UPDATE webhook_receipts SET state = :s, processed_at = now() WHERE id = :id"),
        {"s": state, "id": receipt},
    )
    await db.commit()
    logger.info("github webhook", extra={"event": event, "state": state, "connections_changed": changed})
    return JSONResponse({"state": state}, status_code=202)
