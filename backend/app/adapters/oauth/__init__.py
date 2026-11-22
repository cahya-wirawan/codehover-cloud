"""Sign-in providers (09). Identity is the provider's numeric/opaque subject, never an email."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

ProviderName = Literal["google", "github"]


@dataclass(frozen=True)
class VerifiedIdentity:
    provider: ProviderName
    issuer: str
    subject: str
    display_name: str
    verified_email: str | None  # only an address the provider marks as verified
    handle_hint: str | None = None  # suggests the person's handle (0038): only the public GitHub login


@dataclass(frozen=True)
class AuthorizationRequest:
    state: str
    code_challenge: str
    redirect_uri: str
    nonce: str | None = None


class OAuthError(Exception):
    """Sign-in failed at the provider boundary. `reason` is a safe code; details go to logs only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OAuthProvider(Protocol):
    name: ProviderName
    uses_nonce: bool

    def authorization_url(self, request: AuthorizationRequest) -> str: ...

    async def exchange(
        self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str | None
    ) -> VerifiedIdentity: ...
