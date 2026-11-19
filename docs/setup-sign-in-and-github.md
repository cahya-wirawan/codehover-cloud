# Setting up sign-in and GitHub access

A live deployment needs three registrations with outside providers. The demo (`tests.e2e.demo`) and the test suites fake all three, so they need none of this.

| Registration | What it does | Settings it fills |
|---|---|---|
| Google OAuth client | "Continue with Google" | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` |
| GitHub OAuth app | "Continue with GitHub", and linking GitHub on the Account page | `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET` |
| GitHub App | Read access to the repositories a workspace connects | `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET`, the private key file, `GITHUB_WEBHOOK_SECRET` |

The GitHub OAuth app and the GitHub App are separate on purpose (`09_AUTH_AND_PERMISSIONS.md`): signing in never grants repository access.

**Public repositories need none of this.** A project can read a public GitHub repository without the App: choose "A public GitHub repository" under New project. Optionally set `GITHUB_PUBLIC_TOKEN` to a token of a dedicated GitHub account with no repository access and no permissions. It only raises GitHub's metadata rate limit for the server, from 60 to 5,000 calls an hour; source downloads never use it. You still need the App for private repositories and for push rebuilds.

## Before you start

- [ ] **Decide the environments.** Register a separate set for each environment (local, staging, production). Every registration points at one exact origin, and a GitHub App has one setup URL and one webhook URL.
- [ ] **Fix the public origin.** Use the exact URL people will type, with no trailing slash, for example `https://app.example.com` (locally, the Compose stack uses `http://localhost:8080`). Below, `<origin>` means this value. It goes into `PUBLIC_ORIGIN`, and every callback URL is built from it.
- [ ] **Use HTTPS outside local.** Set `SESSION_COOKIE_SECURE=true` there. Google and GitHub accept plain `http` only for `localhost`.
- [ ] **Keep secrets out of the repository.** Client secrets, the webhook secret and the App private key go into `deployment/.env` (git-ignored) or your secret manager, never into tracked files.

## 1. Google OAuth client

In Google Cloud Console, use a project owned by your organization.

- [ ] **Configure the OAuth consent screen.**
  - App name, support email, and the privacy and terms links (`<origin>/privacy`, `<origin>/terms`).
  - Scopes: `openid`, `email`, `profile` only. These are non-sensitive, so no Google verification review is needed for them.
  - While the app is in "Testing", only listed test users can sign in. Publish it before opening sign-up.
- [ ] **Create the client.** Credentials → Create credentials → OAuth client ID → Web application.
  - Authorized redirect URI: `<origin>/api/v1/auth/google/callback`
  - JavaScript origins are not needed: the server does the exchange.
- [ ] **Copy the credentials.** Put the client ID and client secret into `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

## 2. GitHub OAuth app (sign-in)

Register it under the GitHub organization that will own the product, not a personal account: Settings → Developer settings → OAuth Apps → New OAuth App.

- [ ] **Basic details.**
  - Homepage URL: `<origin>`
  - Authorization callback URL: `<origin>/api/v1/auth/github/callback`
  - Leave "Enable Device Flow" off.
- [ ] **Scopes.** The app asks for `read:user user:email` at sign-in; nothing to configure here.
- [ ] **Copy the credentials.** Generate a client secret, then put the client ID and the secret into `GITHUB_OAUTH_CLIENT_ID` and `GITHUB_OAUTH_CLIENT_SECRET`.

## 3. GitHub App (repository access)

In the same organization: Settings → Developer settings → GitHub Apps → New GitHub App.

- [ ] **Name.** Choose the GitHub App name. People see it on GitHub's install page, and it becomes the slug in `github.com/apps/<slug>`.
- [ ] **Homepage URL:** `<origin>`
- [ ] **Callback URL:** `<origin>/api/v1/integrations/github/callback`
  - Leave "Expire user authorization tokens" on (the default). codehover uses the user token once, during connect, and discards it.
  - Turn **off** "Request user authorization (OAuth) during installation". codehover starts that authorization itself after setup, with its own state and PKCE.
  - Leave "Enable Device Flow" off.
- [ ] **Setup URL:** `<origin>/api/v1/integrations/github/setup`
  - Leave "Redirect on update" **off**. GitHub sends that redirect without codehover's state value, so the setup endpoint would refuse it. To change which repositories a workspace may read, use Connect GitHub again in codehover; reconnecting replaces the grants.
- [ ] **Webhook.**
  - Active: on.
  - Webhook URL: `<origin>/api/v1/webhooks/github`
  - Webhook secret: a long random value (for example `openssl rand -hex 32`). Put it into `GITHUB_WEBHOOK_SECRET`. Without it the endpoint refuses every delivery.
- [ ] **Permissions.** Grant exactly these and nothing else:
  - Repository → Contents: **Read-only**
  - Repository → Metadata: **Read-only** (mandatory)
  - No account permissions, and no write, workflow or administration permission.
- [ ] **Subscribe to events.**
  - **Push**, only if you want "Rebuild on push". It needs no extra permission.
  - Installation events (deleted, suspended, unsuspended) are always delivered to an App; codehover uses them to revoke or suspend connections immediately.
- [ ] **Where can it be installed?**
  - "Any account" lets other people and organizations connect their repositories. A multi-tenant service needs this.
  - "Only on this account" is fine for a private beta inside your own organization.
- [ ] **Copy the IDs and secrets.** After creating the App:
  - App ID → `GITHUB_APP_ID`
  - The slug from `github.com/apps/<slug>` → `GITHUB_APP_SLUG`
  - Client ID → `GITHUB_APP_CLIENT_ID`
  - Generate a client secret → `GITHUB_APP_CLIENT_SECRET`
- [ ] **Generate the private key.** Generate a private key and download the `.pem` file.
  - Locally, save it as `deployment/secrets/github-app.pem` (git-ignored), or point `GITHUB_APP_PRIVATE_KEY_HOST_PATH` at it. Compose mounts it at `/run/secrets/github_app_key`.
  - In production, store it in your secret manager and mount it at the path in `GITHUB_APP_PRIVATE_KEY_FILE`.
  - Repository access stays switched off until the App ID, slug, client ID, client secret and a real key file are all set. The empty placeholder file disables it.

## 4. Start and verify

- [ ] **Restart the services that read these settings.** These are the API and the worker: `docker compose --env-file .env -f compose.yaml --profile app up -d`.
- [ ] **Check readiness:** `<origin>/api/v1/health/ready` answers 200.
- [ ] **Google sign-in.** "Continue with Google" lands you back signed in.
- [ ] **GitHub sign-in.** Sign out, then "Continue with GitHub" does the same. Alternatively, link GitHub from the Account page while signed in with Google. Connecting repositories requires a linked GitHub identity.
- [ ] **Connect GitHub.** In a workspace you administer, open GitHub connections → Connect GitHub.
  - The real GitHub install page opens. Choose an account and a few repositories.
  - You return to GitHub connections with "GitHub connected." and the connection listed.
  - This is the first run of the connect flow against real GitHub (the spec's "confirm against a real App before beta"), so check it in staging first.
- [ ] **Check granted repositories.** Create a project and check that only the repositories you chose are offered.
- [ ] **Free build.** Start a free source-only build. It downloads the snapshot through the App's installation token.
- [ ] **Webhooks.** In the App's Advanced tab, Recent deliveries should show answers of `202`. A `401` means the webhook secret differs between GitHub and `GITHUB_WEBHOOK_SECRET`.
- [ ] **Push rebuilds** (if subscribed). Turn on "Rebuild on push" on the project page, push a commit, and look for it in the project's push history.

## Rotation and incidents

- **Rotate a client secret.** Generate a new one on the provider, update the setting, and restart the API. Then delete the old secret on the provider.
- **Rotate the App private key.** Generate a new key, mount it, restart the API and the worker, then delete the old key on GitHub. Installation tokens are cached in memory only until shortly before they expire, so nothing else needs clearing.
- **Rotate the webhook secret.** Change it on GitHub and in `GITHUB_WEBHOOK_SECRET` together. Redeliver anything that failed in between from Recent deliveries; deliveries are deduplicated by their ID.
- **A customer uninstalls the App.** The installation webhook revokes the connection immediately. Their projects keep their existing builds but cannot take new snapshots until someone reconnects.
- **Known limitation.** A connector who later loses access to a repository on GitHub keeps the workspace's grant until someone reconnects (`09_AUTH_AND_PERMISSIONS.md`).
