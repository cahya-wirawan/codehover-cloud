"""Sign-in, linking and session management (09). Transactions never span provider HTTP calls (04)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.oauth import AuthorizationRequest, OAuthProvider, VerifiedIdentity
from app.core.request_context import current_request_id
from app.core.security import Sealer, csrf_token, digest, new_token, pkce_pair
from app.core.settings import Settings
from app.core.tenancy import set_context
from app.modules.identity.sessions import ABSOLUTE_LIFETIME, Principal

logger = logging.getLogger(__name__)

OAUTH_TTL_SECONDS = 600


class AuthFlowError(Exception):
    """A sign-in or link attempt cannot continue. `code` is shown to the user via the login page."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CompletedAuthorization:
    purpose: str
    return_path: str
    initiating_session_id: UUID | None
    identity: VerifiedIdentity


def redirect_uri(settings: Settings, provider: str) -> str:
    return f"{settings.public_origin}/api/v1/auth/{provider}/callback"


async def begin_authorization(
    db: AsyncSession,
    settings: Settings,
    sealer: Sealer,
    provider: OAuthProvider,
    *,
    purpose: str,
    return_path: str,
    initiating_session_id: UUID | None = None,
) -> str:
    state = new_token()
    nonce = new_token() if provider.uses_nonce else None
    verifier, challenge = pkce_pair()
    state_hash = digest(state)
    sealed = sealer.seal(json.dumps({"verifier": verifier, "nonce": nonce}).encode(), state_hash)
    await db.execute(
        text(
            "SELECT app_private.oauth_transaction_create(:state, :provider, :purpose, :nonce_hash, :sealed,"
            " :return_path, :session, NULL, make_interval(secs => :ttl))"
        ),
        {
            "state": state_hash,
            "provider": provider.name,
            "purpose": purpose,
            "nonce_hash": digest(nonce) if nonce else None,
            "sealed": sealed,
            "return_path": return_path,
            "session": initiating_session_id,
            "ttl": OAUTH_TTL_SECONDS,
        },
    )
    await db.commit()
    return provider.authorization_url(
        AuthorizationRequest(
            state=state,
            code_challenge=challenge,
            redirect_uri=redirect_uri(settings, provider.name),
            nonce=nonce,
        )
    )


async def complete_authorization(
    db: AsyncSession, settings: Settings, sealer: Sealer, provider: OAuthProvider, *, state: str, code: str
) -> CompletedAuthorization:
    state_hash = digest(state)
    row = (
        await db.execute(
            text(
                "SELECT purpose, nonce_hash, encrypted_pkce_verifier, return_path, initiating_session_id"
                " FROM app_private.oauth_transaction_consume(:state, :provider)"
            ),
            {"state": state_hash, "provider": provider.name},
        )
    ).one_or_none()
    # Commit the consumption before calling the provider: the state is single-use even if the exchange
    # fails, and no transaction stays open across external HTTP.
    await db.commit()
    if row is None:
        raise AuthFlowError("expired")
    secrets_: dict[str, Any] = json.loads(sealer.open(row.encrypted_pkce_verifier, state_hash))
    nonce = secrets_.get("nonce")
    if (nonce is None) != (row.nonce_hash is None) or (nonce and digest(nonce) != row.nonce_hash):
        raise AuthFlowError("expired")
    identity = await provider.exchange(
        code=code,
        code_verifier=secrets_["verifier"],
        redirect_uri=redirect_uri(settings, provider.name),
        nonce=nonce,
    )
    return CompletedAuthorization(row.purpose, row.return_path, row.initiating_session_id, identity)


async def _audit(
    db: AsyncSession,
    actor: UUID,
    action: str,
    resource_type: str,
    resource_id: UUID | None,
    metadata: dict[str, Any],
) -> None:
    await db.execute(
        text(
            "INSERT INTO audit_events(workspace_id, actor_user_id, action, resource_type, resource_id,"
            " metadata, request_id) VALUES (NULL, :actor, :action, :rtype, :rid, CAST(:meta AS jsonb), :req)"
        ),
        {
            "actor": actor,
            "action": action,
            "rtype": resource_type,
            "rid": resource_id,
            "meta": json.dumps(metadata),
            "req": current_request_id(),
        },
    )


async def _revoke(db: AsyncSession, principal: Principal) -> None:
    await set_context(db, user_id=principal.user_id)
    await db.execute(
        text("UPDATE sessions SET revoked_at = now() WHERE id = :id AND revoked_at IS NULL"),
        {"id": principal.session_id},
    )


async def sign_in(
    db: AsyncSession, settings: Settings, identity: VerifiedIdentity, previous: Principal | None
) -> str:
    """Find or create the user for this provider subject and start a new session (rotation)."""
    params = {"p": identity.provider, "i": identity.issuer, "s": identity.subject}
    user_id = (await db.execute(text("SELECT app_private.identity_user(:p, :i, :s)"), params)).scalar_one()
    created = False
    if user_id is None:
        try:
            user_id = (
                await db.execute(
                    text("SELECT app_private.create_user_with_identity(:p, :i, :s, :name, :email)"),
                    {**params, "name": identity.display_name, "email": identity.verified_email},
                )
            ).scalar_one()
            created = True
        except exc.IntegrityError as error:  # subject exists but its user is not active
            await db.rollback()
            raise AuthFlowError("unavailable") from error
    if previous is not None:
        await _revoke(db, previous)
    token = new_token()
    await set_context(db, user_id=user_id)
    session_id = (
        await db.execute(
            text(
                "INSERT INTO sessions(user_id, token_hash, csrf_hash, expires_at)"
                " VALUES (:u, :t, :c, now() + make_interval(secs => :life)) RETURNING id"
            ),
            {
                "u": user_id,
                "t": digest(token),
                "c": digest(csrf_token(settings.session_secret.get_secret_value(), token)),
                "life": int(ABSOLUTE_LIFETIME.total_seconds()),
            },
        )
    ).scalar_one()
    await _audit(
        db,
        user_id,
        "session.created",
        "session",
        session_id,
        {"provider": identity.provider, "new_user": created},
    )
    await db.commit()
    return token


async def link_identity(db: AsyncSession, principal: Principal, identity: VerifiedIdentity) -> None:
    """Attach another provider to the signed-in user. Never merges accounts (09)."""
    params = {"p": identity.provider, "i": identity.issuer, "s": identity.subject}
    owner = (await db.execute(text("SELECT app_private.identity_user(:p, :i, :s)"), params)).scalar_one()
    if owner == principal.user_id:
        return
    if owner is not None:
        raise AuthFlowError("link_conflict")
    await set_context(db, user_id=principal.user_id)
    try:
        identity_id = (
            await db.execute(
                text(
                    "INSERT INTO oauth_identities(user_id, provider, issuer, subject, verified_email)"
                    " VALUES (:u, :p, :i, :s, :e) RETURNING id"
                ),
                {**params, "u": principal.user_id, "e": identity.verified_email},
            )
        ).scalar_one()
    except exc.IntegrityError as error:  # claimed concurrently, or by an inactive account
        await db.rollback()
        raise AuthFlowError("link_conflict") from error
    await _audit(
        db,
        principal.user_id,
        "identity.linked",
        "oauth_identity",
        identity_id,
        {"provider": identity.provider},
    )
    await db.commit()


async def sign_out(db: AsyncSession, principal: Principal) -> None:
    await _revoke(db, principal)
    await _audit(
        db, principal.user_id, "session.revoked", "session", principal.session_id, {"reason": "logout"}
    )
    await db.commit()


async def profile(db: AsyncSession, principal: Principal) -> dict[str, Any]:
    await set_context(db, user_id=principal.user_id)
    user = (
        await db.execute(
            text("SELECT id, display_name, primary_email FROM users WHERE id = :u"), {"u": principal.user_id}
        )
    ).one()
    identities = (
        await db.execute(
            text(
                "SELECT id, provider, verified_email, created_at FROM oauth_identities"
                " WHERE user_id = :u ORDER BY created_at"
            ),
            {"u": principal.user_id},
        )
    ).all()
    return {
        "id": user.id,
        "display_name": user.display_name,
        "primary_email": user.primary_email,
        "identities": [dict(r._mapping) for r in identities],
    }


async def list_sessions(db: AsyncSession, principal: Principal) -> list[dict[str, Any]]:
    await set_context(db, user_id=principal.user_id)
    rows = (
        await db.execute(
            text(
                "SELECT id, created_at, last_seen_at, expires_at FROM sessions"
                " WHERE user_id = :u AND revoked_at IS NULL AND expires_at > now() ORDER BY created_at DESC"
            ),
            {"u": principal.user_id},
        )
    ).all()
    return [{**dict(r._mapping), "current": r.id == principal.session_id} for r in rows]


async def revoke_session(db: AsyncSession, principal: Principal, session_id: UUID) -> bool:
    await set_context(db, user_id=principal.user_id)
    revoked = (
        await db.execute(
            text("UPDATE sessions SET revoked_at = now() WHERE id = :id AND revoked_at IS NULL RETURNING id"),
            {"id": session_id},
        )
    ).one_or_none()
    if revoked is None:
        return False
    await _audit(db, principal.user_id, "session.revoked", "session", session_id, {"reason": "user"})
    await db.commit()
    return True


async def unlink_identity(db: AsyncSession, principal: Principal, identity_id: UUID) -> str:
    """Returns "removed", "not_found" or "last_identity" (the final sign-in method cannot be removed)."""
    await set_context(db, user_id=principal.user_id)
    # Lock this user's identities so two concurrent unlinks cannot remove the last two.
    rows = (
        await db.execute(
            text("SELECT id FROM oauth_identities WHERE user_id = :u FOR UPDATE"), {"u": principal.user_id}
        )
    ).all()
    ids = {r.id for r in rows}
    if identity_id not in ids:
        return "not_found"
    if len(ids) == 1:
        return "last_identity"
    await db.execute(text("DELETE FROM oauth_identities WHERE id = :id"), {"id": identity_id})
    await _audit(db, principal.user_id, "identity.unlinked", "oauth_identity", identity_id, {})
    await db.commit()
    return "removed"


def now() -> datetime:
    return datetime.now(UTC)
