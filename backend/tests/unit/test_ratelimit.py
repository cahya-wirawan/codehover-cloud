"""Client addresses for rate limits: forwarded headers count only from a trusted proxy (22)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app.core.ratelimit import bucket, client_address
from tests.conftest import make_settings


def request(peer: str, forwarded: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


def test_forwarded_addresses_count_only_from_trusted_proxies() -> None:
    trusted = make_settings(trusted_proxies="172.16.0.0/12, 10.0.0.0/8")
    assert client_address(request("172.18.0.5", "203.0.113.9"), trusted) == "203.0.113.9"
    assert client_address(request("172.18.0.5", "1.1.1.1, 203.0.113.9"), trusted) == "203.0.113.9"  # our hop
    assert client_address(request("172.18.0.5", "not-an-ip"), trusted) == "172.18.0.5"
    assert client_address(request("198.51.100.7", "203.0.113.9"), trusted) == "198.51.100.7"  # spoof ignored
    assert client_address(request("198.51.100.7", "203.0.113.9"), make_settings()) == "198.51.100.7"


def test_buckets_are_keyed_hmacs_and_bad_networks_are_refused() -> None:
    settings = make_settings()
    key = bucket(settings, "public", "203.0.113.9")
    assert len(key) == 32 and "203" not in key
    assert key != bucket(settings, "signin", "203.0.113.9")
    assert key != bucket(make_settings(session_secret="t" * 32), "public", "203.0.113.9")
    with pytest.raises(ValidationError):
        make_settings(trusted_proxies="everyone")


def test_ipv6_clients_share_their_64_and_mapped_ipv4_is_plain_ipv4() -> None:
    """Security review of 0.5.18: rotating addresses inside one /64 must not buy new buckets."""
    trusted = make_settings(trusted_proxies="172.16.0.0/12")
    a = client_address(request("172.18.0.5", "2001:db8:1:2::1"), trusted)
    b = client_address(request("172.18.0.5", "2001:db8:1:2:ffff:ffff:ffff:fffe"), trusted)
    other = client_address(request("172.18.0.5", "2001:db8:1:3::1"), trusted)
    assert a == b == "2001:db8:1:2::/64" and other == "2001:db8:1:3::/64"
    assert client_address(request("172.18.0.5", "::ffff:203.0.113.9"), trusted) == "203.0.113.9"
    assert client_address(request("2001:db8:9:9::7"), make_settings()) == "2001:db8:9:9::/64"  # the peer too
