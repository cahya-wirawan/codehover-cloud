"""OAuth client ID metadata documents (MCP 2025-11-25 "Client ID Metadata Documents";
draft-ietf-oauth-client-id-metadata-document): an MCP client may use an `https://` URL as its `client_id`,
and the authorization server fetches the JSON document at that URL instead of a registration.

The URL comes from whoever starts a sign-in, so fetching it must never reach the deployment's own network
(SSRF, 08): only `https` on port 443 to a DNS name (never an IP literal), every address the name resolves
to must be public, and the request goes to the checked address itself (the name is sent as Host and TLS
SNI, and the certificate is verified for it), so a second DNS answer cannot redirect it. No redirects are
followed; the answer is capped at 64 KiB and the whole fetch at 5 seconds (a deadline over resolving,
connecting and reading, so a host sending a byte at a time cannot stretch it; security review), over a
small pool of its own. Nothing from the deployment is sent: no cookies,
no credentials, a fixed User-Agent.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

MAX_URL_CHARS = 512
MAX_BYTES = 64 * 1024
TIMEOUT = httpx.Timeout(5.0, connect=3.0)
DEADLINE_SECONDS = 5.0
USER_AGENT = "codehover-cloud (client metadata)"

Resolver = Callable[[str], Awaitable[list[str]]]


class MetadataError(Exception):
    """The document could not be fetched or is not a client metadata document; `reason` is a code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ClientMetadataFetcher(Protocol):
    async def fetch(self, url: str) -> dict[str, Any]: ...


def is_metadata_client_id(value: str) -> bool:
    """Whether a `client_id` names a metadata document: an https URL with a path, a DNS host name, no
    port other than 443, no user info, fragment or dot segments."""
    if not value.startswith("https://") or len(value) > MAX_URL_CHARS or any(c.isspace() for c in value):
        return False
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname or ""
    if parts.username or parts.password or parts.fragment or port not in (None, 443):
        return False
    if not host or "." not in host or not all(c.isalnum() or c in ".-" for c in host):
        return False
    try:
        ipaddress.ip_address(host)
        return False  # an address, not a name
    except ValueError:
        pass
    segments = parts.path.split("/")
    return len(parts.path) > 1 and "." not in segments and ".." not in segments


async def _resolve(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


class HttpClientMetadata:
    """Fetches documents over the app's shared client, pinned to a checked public address."""

    def __init__(self, http: httpx.AsyncClient, resolve: Resolver = _resolve) -> None:
        self.http = http
        self.resolve = resolve

    async def fetch(self, url: str) -> dict[str, Any]:
        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                return await self._fetch(url)
        except TimeoutError as error:
            raise MetadataError("timeout") from error

    async def _fetch(self, url: str) -> dict[str, Any]:
        if not is_metadata_client_id(url):
            raise MetadataError("invalid_url")
        parts = urlsplit(url)
        host = parts.hostname or ""
        try:
            found = await asyncio.wait_for(self.resolve(host), timeout=3.0)
            addresses = [ipaddress.ip_address(a) for a in found]
        except (OSError, ValueError) as error:
            raise MetadataError("unresolvable") from error
        if not addresses or not all(a.is_global for a in addresses):
            raise MetadataError("not_public")
        address = next((a for a in addresses if a.version == 4), addresses[0])
        literal = str(address) if address.version == 4 else f"[{address}]"
        target = f"https://{literal}{parts.path}" + (f"?{parts.query}" if parts.query else "")
        try:
            async with self.http.stream(
                "GET",
                target,
                headers={"Host": host, "Accept": "application/json", "User-Agent": USER_AGENT},
                extensions={"sni_hostname": host},
                timeout=TIMEOUT,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise MetadataError("status")
                if response.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                    raise MetadataError("content_type")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise MetadataError("too_large")
        except httpx.HTTPError as error:
            raise MetadataError("unreachable") from error
        try:
            document = json.loads(bytes(body))
        except ValueError as error:
            raise MetadataError("not_json") from error
        if not isinstance(document, dict):
            raise MetadataError("not_json")
        return document
