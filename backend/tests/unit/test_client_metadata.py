"""Fetching OAuth client ID metadata documents safely (app.adapters.oauth.client_metadata, 0043)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.adapters.oauth.client_metadata import HttpClientMetadata, MetadataError, is_metadata_client_id

URL = "https://client.example.test/oauth/client.json"


def test_only_https_urls_of_names_with_a_path_are_metadata_client_ids() -> None:
    assert is_metadata_client_id(URL)
    assert is_metadata_client_id("https://client.example.test:443/c.json")
    for bad in (
        "chc_abc",
        "http://client.example.test/c.json",
        "https://client.example.test",
        "https://client.example.test/",
        "https://127.0.0.1/c.json",
        "https://[::1]/c.json",
        "https://localhost/c.json",
        "https://client.example.test:8443/c.json",
        "https://user@client.example.test/c.json",
        "https://client.example.test/c.json#x",
        "https://client.example.test/a/../c.json",
        "https://client.example.test/" + "x" * 600,
    ):
        assert not is_metadata_client_id(bad), bad


def fetcher(handler: object, addresses: list[str]) -> HttpClientMetadata:
    async def resolve(host: str) -> list[str]:
        return addresses

    return HttpClientMetadata(httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolve)  # type: ignore[arg-type]


async def test_the_request_goes_to_the_checked_address_with_the_name_as_host() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"client_id": URL}, headers={"Content-Type": "application/json"})

    assert await fetcher(handler, ["93.184.215.14"]).fetch(URL) == {"client_id": URL}
    assert str(seen[0].url) == "https://93.184.215.14/oauth/client.json"
    assert seen[0].headers["host"] == "client.example.test"
    assert seen[0].extensions["sni_hostname"] == "client.example.test"
    assert "cookie" not in seen[0].headers and "authorization" not in seen[0].headers


@pytest.mark.parametrize(
    "addresses", [["10.0.0.5"], ["93.184.215.14", "127.0.0.1"], ["169.254.169.254"], ["::1"], ["fd00::1"], []]
)
async def test_names_that_resolve_to_any_private_address_are_refused(addresses: list[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("never requested")

    with pytest.raises(MetadataError) as error:
        await fetcher(handler, addresses).fetch(URL)
    assert error.value.reason == "not_public"


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(302, headers={"Location": "https://10.0.0.5/x"}), "status"),
        (httpx.Response(404), "status"),
        (httpx.Response(200, text="<html>", headers={"Content-Type": "text/html"}), "content_type"),
        (httpx.Response(200, content=b"[1]", headers={"Content-Type": "application/json"}), "not_json"),
        (
            httpx.Response(
                200, content=b"{" + b" " * 70_000 + b"}", headers={"Content-Type": "application/json"}
            ),
            "too_large",
        ),
        (
            httpx.Response(
                200, content=json.dumps({"a": 1}).encode()[:-1], headers={"Content-Type": "application/json"}
            ),
            "not_json",
        ),
    ],
)
async def test_bad_answers_are_refused(response: httpx.Response, reason: str) -> None:
    with pytest.raises(MetadataError) as error:
        await fetcher(lambda request: response, ["93.184.215.14"]).fetch(URL)
    assert error.value.reason == reason
