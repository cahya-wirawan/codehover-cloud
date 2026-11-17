# Authentication and permissions

## Sign-in and session design

Google: authorization-code OIDC with `openid email profile`, exact redirect URI, state and nonce, server token exchange, discovery/JWKS verification of issuer/audience/expiry/nonce. GitHub: authorization-code OAuth with minimal identity scopes `read:user user:email`; identity is numeric provider user ID, not mutable username or email. Use PKCE S256 where provider supports it, alongside confidential client exchange and state. GitHub login is OAuth, not an assumed OIDC ID token flow.

Opaque session cookie `__Host-codehover_session`: Secure, HttpOnly, SameSite=Lax, Path=/, no Domain. Store only SHA-256 digest of ≥256-bit random token in PostgreSQL; rotate at login, privilege change and reauthentication. Absolute lifetime 30 days, idle expiry 7 days; sensitive ownership/provider-link/account-delete actions require sign-in within 10 minutes. CSRF token bound to session is exposed to same-origin frontend and returned as `X-CSRF-Token`; validate on unsafe methods plus Origin. The token is HMAC-SHA256 over the session token, keyed by an HKDF derivation of `SESSION_SECRET`, so `/auth/csrf` can re-derive it without storing it, and only its digest is kept. Origin must equal `PUBLIC_ORIGIN` exactly. `last_seen_at` is refreshed at most every 5 minutes. Signing in again while signed in rotates the session and counts as the reauthentication needed for linking or unlinking. Clear cookie/query cache on logout; logout-all revokes sessions. Development localhost HTTP may use a different explicitly insecure cookie name (`codehover_session_dev`), never leak that config into production.

State and nonce are one-use, expire after 10 minutes and bind provider, purpose, return path and session if linking. Store PKCE verifier encrypted server-side. OAuth access token never goes into browser local/session storage. GitHub login token is used to verify identity/installation authority where needed, then discarded unless required for a documented repository connection operation; durable repository ingestion uses installation token. Tokens/authorization codes never appear in logs.

Never merge users merely because emails match. If a provider subject already belongs to another user, reject linking and provide account recovery. To link another provider, require an existing session plus reauthentication and bind callback to that session. Email invitation acceptance requires a matching verified email from a linked provider; if none exists, user must verify an address via a separate transactional email flow before joining. Do not assume GitHub public email is verified.

## Repository authorization

Sign-in grants a codehover-cloud identity. GitHub App installation grants repository reading authority, separately. App permissions: Metadata read and Contents read; no write, workflow or administration permission. Webhook subscriptions only for necessary installation/repository lifecycle, plus push in P1. Before associating installation with workspace, validate the initiating linked GitHub user's authority over that installation and selected repository using provider APIs; install callback state is necessary but insufficient. GitHub only answers "which installations of this App can this user access" (`GET /user/installations`, `GET /user/installations/{id}/repositories`) for a user access token issued to the **GitHub App itself**; a token from the separate identity OAuth app cannot prove it. So the setup flow obtains a short-lived GitHub App user access token, either through the App's "request user authorization during installation" option or a GitHub App authorization redirect immediately after the setup callback, using `GITHUB_APP_CLIENT_ID`/`GITHUB_APP_CLIENT_SECRET`, state and PKCE. It then requires: the token's GitHub user ID equals the GitHub identity linked to the current session, the installation ID appears in that user's accessible installations, and every selected repository appears in the installation's accessible repositories. The token is discarded afterwards. Verify the exact App settings (setup vs callback URL, state propagation) against current GitHub documentation in M1. Revalidate on import/build/publish; connection cache max five minutes, revocation webhook takes effect immediately. Installation tokens are short-lived, kept only in memory for authorized acquisition and never in clone URLs persisted to disk or logs.

## Permission matrix

| Action | Viewer | Editor | Admin | Owner |
|---|---|---|---|---|
| Read workspace projects, code, guide, status | Yes | Yes | Yes | Yes |
| Create projects, configure, estimate/build/cancel | No | Yes | Yes | Yes |
| Export complete source/KB/static bundle | No | Yes | Yes | Yes |
| Publish/unpublish, public disclosure, waiver | No | No | Yes | Yes |
| Connect/disconnect GitHub, delete project | No | No | Yes | Yes |
| Invite/change viewer/editor members | No | No | Yes | Yes |
| Manage admins/owners, transfer ownership | No | No | No | Yes |
| View detailed ledger/audit | No | No | Yes | Yes |
| Checkout, cancel plan, billing portal | No | No | No | Yes |
| Delete workspace | No | No | No | Yes |

All members have access to all workspace projects; warn before invitation/import. Role checks are backend service policy, frontend disabled controls only aid usability. Platform operators have a separate operations role with counts/IDs, not implicit tenant membership. Public visitors access only explicitly published resources via publication resolver, not general build endpoints. Any unpublished build is private regardless of repository visibility.

Membership removal/role downgrade is effective on next HTTP request, SSE reauthorization ≤30s. No long-lived signed source URL bypasses revocation. Owner invariant uses locked workspace transaction; prevent concurrent removal of both owners. Invitations are single-use token hashes, scoped role, seven-day expiry and auditable revocation. Notifications/email provider is adapter-backed; send only system-required invites/build notices after user action, not marketing by default.
