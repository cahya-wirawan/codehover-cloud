"""Tokens, digests, encryption at rest and redirect safety (09_AUTH_AND_PERMISSIONS.md).

- Opaque tokens are 256-bit random values; only their SHA-256 digest is stored.
- The CSRF token is HMAC(SESSION_SECRET, session token): derivable for /auth/csrf without storing it,
  verifiable against the stored digest, and useless without the HttpOnly session cookie.
- Small secrets at rest (PKCE verifiers) use AES-256-GCM with a key derived from APP_ENCRYPTION_KEY via
  HKDF; the associated data binds a ciphertext to its row so it cannot be replayed elsewhere.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

TOKEN_BYTES = 32
NONCE_BYTES = 12


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pkce_pair() -> tuple[str, str]:
    """RFC 7636 S256: (verifier, challenge). 64 random bytes -> 86-char verifier (43..128 allowed)."""
    verifier = b64url(secrets.token_bytes(64))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _derive(secret: str, purpose: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"codehover/" + purpose).derive(
        secret.encode()
    )


def csrf_token(session_secret: str, session_token: str) -> str:
    key = _derive(session_secret, b"csrf")
    return b64url(hmac.new(key, b"csrf:" + session_token.encode(), hashlib.sha256).digest())


def matches_digest(candidate: str | None, expected: bytes) -> bool:
    return candidate is not None and hmac.compare_digest(digest(candidate), expected)


class Sealer:
    """AES-256-GCM sealing for small secrets stored in the database."""

    def __init__(self, encryption_key: str) -> None:
        self._aead = AESGCM(_derive(encryption_key, b"seal"))

    def seal(self, plaintext: bytes, associated: bytes) -> bytes:
        nonce = secrets.token_bytes(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, plaintext, associated)

    def open(self, sealed: bytes, associated: bytes) -> bytes:
        return self._aead.decrypt(sealed[:NONCE_BYTES], sealed[NONCE_BYTES:], associated)


# Same rule as the oauth_transactions.return_path CHECK in migration 0002. Browsers strip tabs and
# newlines from URLs ("/\t/x" becomes the protocol-relative "//x"), so the charset is an allowlist.
_RETURN_PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]*")
_DOT_SEGMENT = re.compile(r"(^|/)\.\.?(/|\?|#|$)")


def safe_return_path(candidate: str | None, default: str = "/") -> str:
    if (
        not candidate
        or len(candidate) > 512
        or not _RETURN_PATH.fullmatch(candidate)
        or "//" in candidate
        or _DOT_SEGMENT.search(candidate)
    ):
        return default
    return candidate
