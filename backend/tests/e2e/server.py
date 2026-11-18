"""Test-only server for the Playwright journey (13 "Critical E2E journey"). Never part of the app.

It runs the real application against the integration-test database with fake Google and GitHub
providers (the same fakes as the API tests), serves the built frontend from the same origin, and runs
an inline worker that drains the outbox like the consumer would. Two test-only routes exist:
`/__e2e__/google` completes the fake provider's consent (the browser test intercepts the redirect to
accounts.google.com and sends it here), and `/__e2e__/seed-github` grants the fixture repositories to
a workspace, standing in for the GitHub App connect flow covered by the API tests.

Usage: TEST_DATABASE_URL=... TEST_MIGRATION_DATABASE_URL=... python -m tests.e2e.server --port 8765
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import secrets
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine

from app.adapters.github.app import GitHubApp
from app.adapters.oauth.google import GoogleProvider
from app.core.db import session_factory
from app.engine import catalog
from app.jobs import runner
from app.jobs.envelope import TaskEnvelope
from app.jobs.stages import Runtime
from app.main import create_app
from app.modules.identity.sessions import CurrentPrincipal
from tests.conftest import make_settings
from tests.integration.auth_support import CLIENT_ID, FakeIdP, ddl
from tests.integration.test_builds import fixture_tarball
from tests.integration.test_github import APP_CLIENT_ID, APP_ID, APP_KEY, FakeGitHub

DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"


def build(port: int) -> FastAPI:
    runtime_url = os.environ["TEST_DATABASE_URL"]
    origin = f"http://127.0.0.1:{port}"
    settings = make_settings(
        database_url=SecretStr(runtime_url),
        public_origin=origin,
        session_cookie_secure=False,  # plain-HTTP localhost: the explicit development cookie (09)
        google_client_id=CLIENT_ID,
        google_client_secret=SecretStr("g-secret"),
    )
    app = create_app(settings)
    idp = FakeIdP()
    gh = FakeGitHub(idp)
    provider_http = httpx.AsyncClient(transport=httpx.MockTransport(idp.handler))
    app.state.oauth_providers = {"google": GoogleProvider(CLIENT_ID, "g-secret", provider_http)}
    app.state.github_app = GitHubApp(
        app_id=APP_ID,
        slug="codehover-e2e",
        client_id=APP_CLIENT_ID,
        client_secret="app-secret",
        private_key_pem=APP_KEY.as_pem(private=True).decode(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(gh.handler)),
    )
    app.state.catalog = catalog.builtin()

    @app.get("/__e2e__/google")
    async def google_consent(request: Request) -> RedirectResponse:
        location = f"https://accounts.google.com/?{request.url.query}"
        idp.google_sub = secrets.token_hex(8)
        idp.google_email = f"e2e-{secrets.token_hex(3)}@example.test"
        code = idp.authorize(location)
        state = request.query_params["state"]
        return RedirectResponse(f"/api/v1/auth/google/callback?code={code}&state={state}", status_code=302)

    @app.post("/__e2e__/seed-github")
    async def seed_github(body: dict[str, str], principal: CurrentPrincipal) -> dict[str, Any]:
        installation = gh.add_installation(idp.github_id)
        for repo in gh.repos[installation]:
            gh.tarballs[repo["id"]] = fixture_tarball()
        connection = ddl(
            "INSERT INTO repository_connections(workspace_id, installation_id, github_account_id, github_account_login,"
            " connected_by) VALUES (:w, :i, 77, 'octo-org', :u) RETURNING id",
            w=body["workspace_id"],
            i=installation,
            u=principal.user_id,
        )[0][0]
        for repo in gh.repos[installation][:1]:
            ddl(
                "INSERT INTO repositories(workspace_id, connection_id, provider_repo_id, owner, name, visibility, default_branch)"
                " VALUES (:w, :c, :r, 'octo-org', :n, 'private', 'main')",
                w=body["workspace_id"],
                c=connection,
                r=repo["id"],
                n=repo["name"],
            )
        return {"connection_id": str(connection)}

    async def worker() -> None:
        rt = Runtime(
            sessions=session_factory(create_async_engine(runtime_url)),
            store=app.state.objects,
            github=app.state.github_app,
            allow_unsandboxed=True,
            catalog=app.state.catalog,
            retry_delays=(0.0,),
        )
        since = ddl("SELECT now()")[0][0]  # leftovers of earlier test runs are not this server's work
        while True:
            rows = ddl(
                "UPDATE outbox SET published_at = now() WHERE published_at IS NULL AND created_at > :since RETURNING payload",
                since=since,
            )
            for (payload,) in rows:
                with contextlib.suppress(Exception):
                    await runner.process(rt, TaskEnvelope.model_validate(payload))
            await asyncio.sleep(0.2)

    original = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(application: Any) -> Any:  # the app has a lifespan, so on_event would be ignored
        async with original(application) as state:
            task = asyncio.create_task(worker())
            try:
                yield state
            finally:
                task.cancel()

    app.router.lifespan_context = lifespan

    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            from app.core.errors import ApiError

            raise ApiError(404, "NOT_FOUND", "Not found.")
        candidate = DIST / path
        if path and candidate.is_file() and DIST in candidate.resolve().parents:
            return FileResponse(candidate)
        return FileResponse(DIST / "index.html")  # client-side routes (deep links)

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run(build(args.port), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
