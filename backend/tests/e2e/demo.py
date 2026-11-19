"""A local demo of the whole product with fake Google and GitHub: test tooling, never part of the app.

    cd backend && set -a && . ../deployment/.env && set +a && .venv/bin/python -m tests.e2e.demo

Then open http://127.0.0.1:8770 (any other host name, such as localhost, is redirected there: the API
accepts changes only from its one configured origin). It runs the real API and the built
frontend (`cd frontend && npm run build` first) on the E2E server, with:
- its own database (`codehover_demo`) and bucket (`codehover-demo`) on the Compose stack, so the stack's
  workers never touch demo jobs and demo data survives restarts;
- "Continue with Google" opening a local page where you choose who to be (no Google involved): four
  people who keep their account across sign-ins, with fixed emails for invitations, or someone new;
- "Connect GitHub" going to local stand-ins for GitHub's install and authorize pages, then through the
  real setup and ownership check against the fake GitHub;
- two repositories connected to every workspace automatically: `demo-api` (a tiny fixture) and
  `codehover-engine` (this project's own engine package, for a bigger read);
- free replay builds only (source-only cards; no model calls).

`--reset` drops the demo database and bucket first.
"""

from __future__ import annotations

import argparse
import html
import io
import os
import secrets
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

BACKEND = Path(__file__).resolve().parents[2]
DATABASE, BUCKET = "codehover_demo", "codehover-demo"
ENGINE = BACKEND / "app" / "engine"
# Who you can sign in as. Each keeps one account (a fixed Google subject) and a verified email, so an
# invitation sent to that email can be accepted by signing in as them.
PEOPLE = {
    "grace": "Grace Hopper",
    "alan": "Alan Turing",
    "ada": "Ada Lovelace",
    "katherine": "Katherine Johnson",
}


def chooser_page(query: str) -> str:
    """The fake Google consent page: one button per person, carrying the sign-in request along."""

    def option(key: str, name: str, email: str | None) -> str:
        href = html.escape(f"/__demo__/google?{urlencode({'as': key})}&{query}")
        detail = html.escape(email) if email else "A new account every time"
        return (
            f'<a class="person" href="{href}"><span class="name">{html.escape(name)}</span>'
            f'<span class="email">{detail}</span></a>'
        )

    people = "".join(option(key, name, f"{key}@demo.test") for key, name in PEOPLE.items())
    return shell(
        "Choose who to sign in as",
        f"""<p>Local demo: no Google account is involved. Each person keeps the same account every time.</p>
{people}
{option("new", "Someone new", None)}
<p class="note">To add someone to a workspace, invite their email under Members, then sign in as them
(a private window keeps both signed in).</p>""",
    )


def shell(title: str, body: str) -> str:
    """A minimal stand-in page for an outside provider (Google, GitHub), in the app's quiet style."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{ color-scheme: light dark; --bg:#f7f7f9; --panel:#fff; --text:#1c1d21; --muted:#5f6370; --border:#e2e3e8; --accent:#4f46e5; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141518; --panel:#1c1d21; --text:#ececf1; --muted:#a0a3ad; --border:#2c2e34; --accent:#8b85ff; }} }}
body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.5 system-ui, sans-serif; }}
main {{ max-width:28rem; margin:12vh auto; padding:0 16px; }}
.box {{ background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:20px; }}
h1 {{ font-size:18px; margin:0 0 4px; }} p {{ color:var(--muted); margin:0 0 16px; }}
.person, .choice {{ display:flex; flex-direction:column; padding:10px 12px; border:1px solid var(--border);
  border-radius:8px; margin-top:8px; color:inherit; text-decoration:none; }}
.choice {{ flex-direction:row; align-items:flex-start; gap:10px; cursor:pointer; }}
.person:hover, .person:focus-visible, .choice:hover {{ border-color:var(--accent); outline:none; }}
.name {{ font-weight:600; }} .email {{ color:var(--muted); font-size:13px; display:block; }}
fieldset {{ border:0; margin:0 0 16px; padding:0; }} legend {{ font-weight:600; padding:0; }}
button {{ font:inherit; font-weight:600; color:#fff; background:var(--accent); border:0; border-radius:8px;
  padding:8px 14px; cursor:pointer; }}
button:focus-visible, input:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
.note {{ margin-top:16px; font-size:13px; }}
</style></head>
<body><main><div class="box">
<h1>{html.escape(title)}</h1>
{body}
</div></main></body></html>"""


def install_page(state: str, accounts: list[tuple[str, str]], repos: list[str]) -> str:
    """The fake GitHub "install the app" page: pick an account and the repositories codehover may read."""
    account_choices = "".join(
        f'<label class="choice"><input type="radio" name="account" value="{html.escape(login)}"'
        f'{" checked" if i == 0 else ""}><span><span class="name">{html.escape(login)}</span>'
        f'<span class="email">{html.escape(kind)}</span></span></label>'
        for i, (login, kind) in enumerate(accounts)
    )
    repo_choices = "".join(
        f'<label class="choice"><input type="checkbox" name="repo" value="{html.escape(name)}" checked>'
        f'<span class="name">{html.escape(name)}</span></label>'
        for name in repos
    )
    return shell(
        "Install codehover on GitHub",
        f"""<p>Local demo: this stands in for GitHub's install page. codehover gets read-only access to
the repositories you choose; GitHub then sends you back to finish connecting.</p>
<form method="get" action="/__demo__/github/installed">
<input type="hidden" name="state" value="{html.escape(state)}">
<fieldset><legend>Install on</legend>{account_choices}</fieldset>
<fieldset><legend>Repository access</legend>{repo_choices}</fieldset>
<button type="submit">Install</button>
</form>""",
    )


def engine_tarball() -> bytes:
    """This project's engine package as a repository archive (local only; it never leaves the machine)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(ENGINE.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            data = path.read_bytes()
            info = tarfile.TarInfo(f"octo-engine-c0ffee/engine/{path.relative_to(ENGINE)}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def prepare(reset: bool) -> None:
    """Create (or, with reset, recreate) the demo database and bucket, migrate, and point the server at them."""
    import boto3

    from tests.e2e.restore_drill import prepare as create_database
    from tests.e2e.restore_drill import sql, storage_env

    env = {**storage_env(), "S3_BUCKET": BUCKET}
    s3 = boto3.client(
        "s3",
        endpoint_url=env["S3_ENDPOINT_URL"],
        aws_access_key_id=env["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=env["S3_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )
    buckets = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
    if reset:
        sql("postgres", f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
        if BUCKET in buckets:
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if keys:
                    s3.delete_objects(Bucket=BUCKET, Delete={"Objects": keys})
            s3.delete_bucket(Bucket=BUCKET)
            buckets.discard(BUCKET)
    if not sql("postgres", f"SELECT 1 FROM pg_database WHERE datname = '{DATABASE}'"):  # noqa: S608 - constant
        create_database(DATABASE)
    if BUCKET not in buckets:
        s3.create_bucket(Bucket=BUCKET)
    ddl = f"postgresql+psycopg://codehover_ddl:{os.environ['POSTGRES_PASSWORD']}@127.0.0.1:55432/{DATABASE}"
    subprocess.run(  # noqa: S603 - fixed command
        [str(BACKEND / ".venv/bin/alembic"), "upgrade", "head"],
        cwd=BACKEND,
        check=True,
        env={**os.environ, "MIGRATION_DATABASE_URL": ddl},
        capture_output=True,
    )
    runtime = f"postgresql+psycopg://app_runtime:{os.environ['APP_DB_PASSWORD']}@127.0.0.1:55432/{DATABASE}"
    # Read when the server and the test helpers are imported, so set before demo_app().
    os.environ.update({**env, "TEST_MIGRATION_DATABASE_URL": ddl, "TEST_DATABASE_URL": runtime})


def demo_app(port: int) -> Any:
    from fastapi import Request
    from starlette.responses import Response

    from app.adapters.oauth.github import ISSUER as GITHUB_ISSUER
    from app.core.security import digest
    from app.modules.identity.sessions import cookie_name
    from tests.e2e import server
    from tests.integration.auth_support import ddl
    from tests.integration.test_builds import fixture_tarball
    from tests.integration.test_github import APP_CLIENT_ID

    app = server.build(port)
    idp, gh = app.state.e2e_fakes
    tarballs = {"demo-api": fixture_tarball(), "codehover-engine": engine_tarball()}

    def register(installation: int, repos: list[tuple[int, str]], owner: str = "octo-org") -> None:
        gh.repos[installation] = [
            {
                "id": rid,
                "name": name,
                "owner": {"login": owner},
                "private": True,
                "visibility": "private",
                "default_branch": "main",
            }
            for rid, name in repos
        ]
        for rid, name in repos:
            gh.tarballs[rid] = tarballs.get(name, tarballs["demo-api"])

    # Connections stored by earlier runs: make the fake GitHub know them again.
    known: dict[int, list[tuple[int, str]]] = {}
    for installation, rid, name in ddl(
        "SELECT c.installation_id, r.provider_repo_id, r.name FROM repository_connections c"
        " JOIN repositories r ON r.connection_id = c.id"
    ):
        known.setdefault(int(installation), []).append((int(rid), name))
    for installation, repos in known.items():
        register(installation, repos)

    def ensure_connection(workspace_id: str) -> None:
        if ddl("SELECT 1 FROM repository_connections WHERE workspace_id = :w", w=workspace_id):
            return
        owner = ddl(
            "SELECT user_id FROM memberships WHERE workspace_id = :w AND role = 'owner' LIMIT 1",
            w=workspace_id,
        )
        if not owner:
            return
        installation = secrets.randbelow(10**9) + 1
        repos = [(secrets.randbelow(10**9) + 1, name) for name in tarballs]
        register(installation, repos)
        connection = ddl(
            "INSERT INTO repository_connections(workspace_id, installation_id, github_account_id, github_account_login,"
            " connected_by) VALUES (:w, :i, 77, 'octo-org', :u) RETURNING id",
            w=workspace_id,
            i=installation,
            u=owner[0][0],
        )[0][0]
        for rid, name in repos:
            ddl(
                "INSERT INTO repositories(workspace_id, connection_id, provider_repo_id, owner, name, visibility,"
                " default_branch) VALUES (:w, :c, :r, 'octo-org', :n, 'private', 'main')",
                w=workspace_id,
                c=connection,
                r=rid,
                n=name,
            )

    # "Connect GitHub": the app's two browser redirects to github.com (install, then authorize) go to
    # local stand-in pages instead; everything after them is the real setup and ownership check.
    github_app = app.state.github_app
    github_app.installation_url = lambda state: f"/__demo__/github/install?{urlencode({'state': state})}"
    github_app.user_authorization_url = lambda **params: (
        "/__demo__/github/authorize?"
        + urlencode({"client_id": APP_CLIENT_ID, "code_challenge_method": "S256", **params})
    )
    settings = app.state.settings

    def signed_in(request: Request) -> tuple[str, str] | None:
        """The signed-in demo user (id, display name), read from the session cookie like the API does."""
        token = request.cookies.get(cookie_name(settings))
        if not token:
            return None
        rows = ddl(
            "SELECT u.id, u.display_name FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash = :h AND s.revoked_at IS NULL AND s.expires_at > now()",
            h=digest(token),
        )
        return (str(rows[0][0]), str(rows[0][1])) if rows else None

    def github_account(user_id: str) -> int:
        """The person's GitHub account: linked to their codehover user on first use, as if they had
        linked GitHub on the Account page (the connect flow requires a linked, matching identity)."""
        rows = ddl(
            "SELECT subject FROM oauth_identities WHERE user_id = :u AND provider = 'github' LIMIT 1",
            u=user_id,
        )
        if rows:
            return int(rows[0][0])
        github_id = secrets.randbelow(10**9) + 1
        ddl(
            "INSERT INTO oauth_identities(user_id, provider, issuer, subject) VALUES (:u, 'github', :i, :s)",
            u=user_id,
            i=GITHUB_ISSUER,
            s=str(github_id),
        )
        return github_id

    def github_login(display_name: str) -> str:
        return "".join(c for c in display_name.lower() if c.isalnum()) or "octocat"

    canonical = f"127.0.0.1:{port}"

    def github_page(request: Request) -> Response:
        from starlette.responses import HTMLResponse, RedirectResponse

        params = parse_qs(request.url.query)
        state = params.get("state", [""])[0]
        person = signed_in(request)
        if person is None:
            return RedirectResponse("/login", status_code=302)
        user_id, display_name = person
        personal = github_login(display_name)
        accounts = [(personal, "Your personal account"), ("octo-labs", "An organization you own")]
        if request.url.path == "/__demo__/github/install":
            page = HTMLResponse(install_page(state, accounts, list(tarballs)))
            page.headers["Cache-Control"] = "no-store"
            return page
        if request.url.path == "/__demo__/github/installed":
            # GitHub creates the installation, then sends the browser to the app's setup URL.
            login = params.get("account", [personal])[0]
            if login not in dict(accounts):
                login = personal
            chosen = [name for name in params.get("repo", []) if name in tarballs] or list(tarballs)
            installation = secrets.randbelow(10**9) + 1
            account_id = int.from_bytes(digest(f"{user_id}:{login}")[:4], "big") + 1
            gh.installations.setdefault(github_account(user_id), []).append(
                {"id": installation, "account": {"id": account_id, "login": login}}
            )
            register(installation, [(secrets.randbelow(10**9) + 1, name) for name in chosen], owner=login)
            setup = urlencode({"installation_id": installation, "setup_action": "install", "state": state})
            return RedirectResponse(f"/api/v1/integrations/github/setup?{setup}", status_code=302)
        if request.url.path == "/__demo__/github/authorize":
            # Already authorized on GitHub: it returns straight to the callback with a code.
            location = f"https://github.com/login/oauth/authorize?{request.url.query}"
            code = gh.authorize(location, github_account(user_id))
            callback = urlsplit(params.get("redirect_uri", ["/api/v1/integrations/github/callback"])[0]).path
            return RedirectResponse(
                f"{callback}?{urlencode({'code': code, 'state': state})}", status_code=302
            )
        return Response(status_code=404)

    @app.middleware("http")
    async def demo(request: Request, call_next: Any) -> Response:
        if request.headers.get("host") != canonical:
            # The API accepts changes only from its own origin (CSRF): `localhost` would load the pages
            # but be refused on every change, so send every other host name to the one origin.
            from starlette.responses import RedirectResponse

            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"http://{canonical}{request.url.path}{query}", status_code=307)
        if request.url.path == "/__demo__/signin":
            from starlette.responses import HTMLResponse

            page = HTMLResponse(chooser_page(request.url.query))
            page.headers["Cache-Control"] = "no-store"
            return page
        if request.url.path == "/__demo__/google":
            # Complete the fake consent as the chosen person; the remaining query is the sign-in request.
            from starlette.responses import RedirectResponse

            params = {k: v[0] for k, v in parse_qs(request.url.query).items()}
            person = params.pop("as", "new")
            if person in PEOPLE:
                idp.google_sub = f"demo-{person}"
                idp.google_email = f"{person}@demo.test"
                idp.google_claims = {"name": PEOPLE[person]}
            else:
                idp.google_sub = secrets.token_hex(8)
                idp.google_email = f"demo-{secrets.token_hex(3)}@demo.test"
                idp.google_claims = {}
            code = idp.authorize(f"https://accounts.google.com/?{urlencode(params)}")
            callback = urlencode({"code": code, "state": params.get("state", "")})
            return RedirectResponse(f"/api/v1/auth/google/callback?{callback}", status_code=302)
        if request.url.path.startswith("/__demo__/github/"):
            return github_page(request)
        parts = request.url.path.split("/")
        # /api/v1/workspaces/{wid}/... : every workspace comes with the demo repositories.
        if len(parts) > 5 and parts[1:4] == ["api", "v1", "workspaces"] and request.method == "GET":
            try:
                from uuid import UUID

                ensure_connection(str(UUID(parts[4])))
            except ValueError:
                pass
        response: Response = await call_next(request)
        location = response.headers.get("location", "")
        if request.url.path == "/api/v1/auth/google/start" and location.startswith(
            "https://accounts.google.com"
        ):
            # Sign in locally: choose who to be on the fake provider's page instead of Google.
            response.headers["location"] = f"/__demo__/signin?{urlsplit(location).query}"
        return response

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Local demo with fake Google and GitHub.")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--reset", action="store_true", help="drop the demo database and bucket first")
    args = parser.parse_args()
    if not (BACKEND.parent / "frontend" / "dist" / "index.html").exists():
        print("build the frontend first: cd frontend && npm run build", file=sys.stderr)
        return 2
    prepare(args.reset)
    import uvicorn

    print(f"codehover demo: http://127.0.0.1:{args.port}  (Ctrl+C to stop)", flush=True)
    uvicorn.run(demo_app(args.port), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
