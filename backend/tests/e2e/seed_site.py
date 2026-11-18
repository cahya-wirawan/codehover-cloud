"""Seed a published replay site on a running E2E server (`tests.e2e.server`) for the load check and the
restore drill. Test tooling only: it signs in through the server's fake Google provider.

    python -m tests.e2e.seed_site --base http://127.0.0.1:8765 [--files 200 --functions 100] [--private]

Prints JSON with the site slug and the workspace, project and build IDs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import time
from urllib.parse import urlsplit

import httpx


async def sign_in(http: httpx.AsyncClient) -> None:
    start = await http.get("/api/v1/auth/google/start", params={"return_to": "/"})
    query = urlsplit(start.headers["location"]).query
    consent = await http.get(f"/__e2e__/google?{query}")
    callback = await http.get(consent.headers["location"])
    assert callback.status_code == 302 and "error=" not in callback.headers["location"], callback.headers


async def send(http: httpx.AsyncClient, method: str, path: str, **kw: object) -> httpx.Response:
    token = (await http.get("/api/v1/auth/csrf")).json()["csrf_token"]
    headers = {"Origin": str(http.base_url).rstrip("/"), "X-CSRF-Token": token, **kw.pop("headers", {})}  # type: ignore[dict-item]
    response = await http.request(method, path, headers=headers, **kw)  # type: ignore[arg-type]
    assert response.status_code < 400, (path, response.status_code, response.text[:300])
    return response


async def seed(base: str, files: int, functions: int, private: bool, wait_seconds: float) -> dict[str, str]:
    result, _cookies = await seed_with_session(base, files, functions, private, wait_seconds)
    return result


async def seed_with_session(
    base: str, files: int = 0, functions: int = 0, private: bool = False, wait_seconds: float = 300
) -> tuple[dict[str, str], httpx.Cookies]:
    """Seed a site and return the admin's session cookies too (the restore drill deletes with them)."""
    async with httpx.AsyncClient(base_url=base, timeout=60) as http:
        return await _seed(http, files, functions, private, wait_seconds), http.cookies


async def _seed(
    http: httpx.AsyncClient, files: int, functions: int, private: bool, wait_seconds: float
) -> dict[str, str]:
    await sign_in(http)
    run = secrets.token_hex(3)
    ws = (
        await send(http, "POST", "/api/v1/workspaces", json={"name": "Load", "slug": f"load-{run}"})
    ).json()["id"]
    await send(
        http,
        "POST",
        "/__e2e__/seed-github",
        json={"workspace_id": ws, "files": files, "functions": functions},
    )
    connection = (await http.get(f"/api/v1/workspaces/{ws}/connections")).json()["items"][0]["id"]
    repo = (
        await http.get(f"/api/v1/workspaces/{ws}/repositories", params={"connection_id": connection})
    ).json()["items"][0]["id"]
    project = (
        await send(
            http,
            "POST",
            f"/api/v1/workspaces/{ws}/projects",
            json={"repository_id": repo, "name": "API", "slug": "api"},
        )
    ).json()["id"]
    build = (
        await send(
            http,
            "POST",
            f"/api/v1/projects/{project}/builds",
            json={},
            headers={"Idempotency-Key": secrets.token_hex(12)},
        )
    ).json()["id"]
    deadline = time.monotonic() + wait_seconds
    while (status := (await http.get(f"/api/v1/builds/{build}")).json()["status"]) not in (
        "ready",
        "failed",
    ):
        assert time.monotonic() < deadline, f"build still {status}"
        await asyncio.sleep(1)
    assert status == "ready", status
    slug = f"load-{run}"
    body = {"build_id": build, "public_slug": slug}
    if not private:
        body |= {"visibility": "public", "confirmation": "API"}
    await send(
        http,
        "POST",
        f"/api/v1/projects/{project}/publication",
        json=body,
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    return {"slug": slug, "workspace_id": ws, "project_id": project, "build_id": build}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--files", type=int, default=0)
    parser.add_argument("--functions", type=int, default=0)
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(seed(args.base, args.files, args.functions, args.private, args.timeout))))


if __name__ == "__main__":
    main()
