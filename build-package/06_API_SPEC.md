# API and event contracts

Base `/api/v1`; the path carries the API MAJOR and OpenAPI `info.version` the full SemVer, with additive-only changes inside a major and `Deprecation`/`Sunset` headers before removal (document 23). JSON UTF-8; UUID strings; RFC3339 UTC timestamps; amounts explicitly named `*_micro_usd`. Same-origin opaque session cookie, CSRF header on unsafe authenticated actions, Origin checks. OAuth callbacks and signed provider webhook endpoints use dedicated validation instead of session CSRF. FastAPI emits authoritative OpenAPI 3.1 from Pydantic models; generated TypeScript client is checked for drift in CI. The tables below specify the full planned MVP contract; `contracts/` contains machine-readable key schema examples, not a claim that an API exists.

## Shared behavior

Error response: `{ "error": { "code": "ESTIMATE_EXPIRED", "message": "Refresh this estimate before building.", "request_id": "...", "retryable": false, "details": {"estimate_id":"..."} } }`. Never return source, credentials, stack traces or provider bodies in errors. Authentication codes include `UNAUTHENTICATED` (401), `ORIGIN_REJECTED`, `CSRF_REJECTED` and `REAUTH_REQUIRED` (403), and `LAST_IDENTITY` (409). All API responses carry `Cache-Control: no-store`. 400 invalid syntax, 401 sign-in, 403 role/CSRF, 404 resource absent/unauthorized, 409 state/idempotency/ETag conflict, 410 expired export, 422 semantic invalid input, 429 throttled with Retry-After, 503 dependency unavailable. Pydantic validation errors are normalized to the envelope.

Collection responses: `{items: [...], next_cursor: "opaque-or-null"}`; default limit 25, max 100. A forged cursor, or one replayed on another resource, → 400 `INVALID_CURSOR`. Cursor encodes sort key `(created_at,id)` and filter digest with integrity signature. Filters/sort are allowlisted. Path is always a query value/opaque file ID, never a filesystem join from a route. URI decoding happens once.

Creation requests for builds, estimates, exports, checkout and publish require `Idempotency-Key` (16–128 chars). Key scoped by principal/workspace/route, body digest stored 24h. Same key/body returns original response; different body returns 409. Retrying transport error uses same key. Configuration writes/publication use `If-Match` ETag. `POST` create returns 201 or 202 asynchronous Location; DELETE generally 202 for purge and 204 for immediate revoke.

## Endpoint inventory

| Method and route | Input → response | Access / side effect |
|---|---|---|
| GET `/auth/{google|github}/start` | `return_to` (relative path, revalidated server-side) → 302 provider | Anonymous; one-time state server stored; unconfigured provider → 302 `/login?error=unavailable` |
| GET `/auth/{provider}/callback` | code/state → 302 app, session cookie | Validated transaction; OIDC checks for Google. Failures redirect to `/login?error=denied\|expired\|provider\|unavailable`; a link conflict redirects to `/account/security?error=link_conflict` |
| GET `/auth/csrf` | none → token | Same-origin; session bound |
| GET `/me` | none → id, profile, linked providers | Authenticated |
| POST `/auth/logout` | none → 204 | Self; revoke and clear cookie |
| GET/DELETE `/me/sessions[/{id}]` | cursor or session → list/204 | Self, revoke current/all supported explicitly |
| POST `/me/identities/{provider}/link` | reauthentication → redirect URL | Self; link callback bound to same session |
| DELETE `/me/identities/{id}` | reauthentication → 204 | Cannot remove final sign-in method |
| GET/POST `/workspaces` | name, slug → workspace (`{items}` with caller's role) | Self; create owner membership; taken slug → 409 `SLUG_TAKEN` |
| GET/PATCH `/workspaces/{wid}` | name + `If-Match` ETag → workspace | Member read; admin write; missing If-Match → 428 `PRECONDITION_REQUIRED`, stale → 409 `VERSION_CONFLICT` |
| GET `/workspaces/{wid}/members` | cursor → members | Member |
| GET/POST `/workspaces/{wid}/invitations` | email, role → invite with one-time `accept_url` (token in the URL fragment, never logged) | Admin; admins invite viewer/editor only, admin/owner invites require owner; duplicate live invite → 409 `INVITATION_EXISTS` |
| DELETE `/workspaces/{wid}/invitations/{id}` | none → 204 | Admin |
| POST `/invitations/accept` | token → membership | Signed-in verified invited email; unknown/used/revoked/expired → 404 `INVITATION_INVALID`, wrong email → 403 `INVITATION_EMAIL_MISMATCH` |
| PATCH/DELETE `/workspaces/{wid}/members/{uid}` | role → 204 / 204 | Admin manages viewer/editor only; owner for admins/owners; any member may leave; last owner → 409 `LAST_OWNER` |
| POST `/workspaces/{wid}/github/install` | none → `install_url` (+ flow cookie) | Admin; App not configured → 503 `GITHUB_APP_UNAVAILABLE` |
| GET `/integrations/github/setup` | installation_id (untrusted), state → 302 GitHub App user authorization (PKCE) | Install-phase state, browser- and session-bound |
| GET `/integrations/github/callback` | code, state → 302 `/w/{wid}/connections?connected=1` or `?error=` | Exchange for a GitHub App user token; require the linked GitHub identity and the installation in `GET /user/installations` (09); re-check admin role; token discarded. Errors: `expired`, `github_identity_required`, `identity_mismatch`, `installation_not_accessible`, `forbidden`, `provider` |
| GET/DELETE `/workspaces/{wid}/connections[/{id}]` | cursor/none → list/204 | Admin revoke; cancels/suspends affected operations |
| GET `/workspaces/{wid}/repositories` | `connection_id` → granted repositories the installation can still read (internal IDs, names refreshed) | Editor; never wider than the connector's grant (09); inactive connection → 409 `CONNECTION_INACTIVE` |
| GET `/workspaces/{wid}/repositories/{rid}/refs` | none → branches and tags with SHAs | Editor; repository addressed by numeric GitHub ID (rename-safe); outside the installation → 404 |
| GET/POST `/workspaces/{wid}/projects` | repository_id,name,tracked_ref → project | Member list; editor create |
| GET/PATCH `/projects/{pid}` | name/config/tracked_ref → project | Viewer read; editor write with ETag |
| POST `/projects/{pid}/snapshots` | ref → snapshot job/status | Editor; acquisition async |
| GET `/projects/{pid}/snapshots/{sid}` | none → SHA,status,scan summary | Member |
| GET `/projects/{pid}/snapshots/{sid}/files` | cursor,status → metadata | Member; content requires viewer endpoint |
| POST `/projects/{pid}/estimates` | snapshot_id, config → estimate | Editor; scan/config bounded |
| GET `/projects/{pid}/estimates/{eid}` | none → estimate | Member |
| GET/POST `/projects/{pid}/builds` | estimate_id, cap_micro_usd → build | Viewer list; editor start |
| GET `/builds/{bid}` | none → states,counts,usage,quality | Member |
| POST `/builds/{bid}/cancel` | reason optional → updated build | Editor; idempotent cancellation |
| POST `/builds/{bid}/retry` | valid estimate_id → new build | Editor; same SHA/config unless new estimate specifies otherwise |
| GET `/builds/{bid}/events` | Last-Event-ID → SSE | Member; cursor resume and periodic permission recheck |
| GET `/builds/{bid}/tree` | parent_path,cursor → entries | Member/public only through published resource resolver |
| GET `/builds/{bid}/files/{fid}` | start_line,end_line → source/card metadata | Member; max 500 source lines/request |
| GET `/builds/{bid}/symbols/{symid}` | none → card,span,relations | Member |
| GET `/builds/{bid}/search` | q,kind,limit → ranked hits | Member; max q 200 chars, limit 50 |
| GET `/builds/{bid}/guide[/{slug}]` | none → index/page and checks | Member |
| GET `/builds/{bid}/quality` | status,cursor → checks/coverage | Member |
| POST `/builds/{bid}/quality/waivers` | claim IDs,reason → waiver | Admin; cannot waive missing deterministic source locations |
| POST `/projects/{pid}/publication` | build_id,visibility,confirmation → publication | Admin; ETag/idempotency; public confirmation |
| DELETE `/projects/{pid}/publication` | `If-Match` publication ETag → 204 | Admin; immediate stop serving; no request body |
| GET `/published/{slug}/...` | resource route → viewer data | Resolver authorizes visibility; shares no private object URL |
| POST `/builds/{bid}/exports` | format static_zip/kb_json,variant → export | Editor default export permission |
| GET `/exports/{eid}` | none → state,expires_at | Requester or admin |
| GET `/exports/{eid}/download` | none → attachment stream | Reauthorize every request; no public bearer URL |
| GET `/workspaces/{wid}/usage` | period → allowance,reserved,settled | Members summary |
| GET `/workspaces/{wid}/usage/entries` | cursor → ledger | Owner/admin |
| POST `/workspaces/{wid}/billing/checkout` | plan_key → checkout_url | Owner; provider-configured price mapping |
| POST `/workspaces/{wid}/billing/portal` | none → portal_url | Owner; fixed allowed return origin |
| GET `/workspaces/{wid}/audit` | cursor,type → audit entries | Admin |
| POST `/projects/{pid}/deletion` | confirmation → request | Admin; disables delivery immediately |
| POST `/me/export` / `/me/deletion` | reauthentication, confirmation → request | Self; ownership transfer rule |
| POST `/webhooks/github` | raw signed body → 202 | HMAC verified; delivery deduplicated |
| POST `/webhooks/stripe` | raw signed body → 200/202 | Signature/timestamp verified; durable receipt then async processing |
| GET `/health/live` / `/health/ready` | none → status, application `version` | Live process only; readiness DB/schema, essential dependencies |

## Core request/response examples

Create estimate request:
```json
{"snapshot_id":"019a90a0-0000-7000-8000-000000000001","config":{"include":["src/**"],"exclude":["**/fixtures/**"],"languages":["en"],"levels":["standard"],"max_files":100,"guide_pages":5,"model_preset":"balanced-v1"}}
```
Response 201:
```json
{"id":"019a90a0-0000-7000-8000-000000000002","commit_sha":"40-or-64-hex-provider-sha","config_digest":"sha256-hex","price_version":"catalog-2026-11-a","selected_files":82,"excluded_files":19,"estimated_tokens":{"input":170000,"output":42000},"cost_micro_usd":{"low":1200000,"high":3900000},"expires_at":"2026-11-17T16:30:00Z","disclaimer":"Illustrative estimate; actual usage is metered."}
```
Cost figures above are synthetic examples, not provider pricing. Real SHA/hash fields must pass strict hexadecimal validation.

Start build request: `{"estimate_id":"019a90a0-0000-7000-8000-000000000002","cap_micro_usd":5000000}`. Response 202 includes `id`, `status:"queued"`, `snapshot_id`, `commit_sha`, `events_url`, `reserved_micro_usd`, `created_at`. Reject expired estimate, scope/price mismatch, suspended connection, exhausted allowance or duplicate active project build before scheduling.

Symbol response fields: id/build_id, symbol_key, file_id, path, language, kind, signature, span `{start_line,end_line,start_column,end_column}` (lines 1-based inclusive; columns 0-based, end-exclusive, counted in Unicode scalar values; the frontend converts to UTF-16 at one tested boundary), card `{summary,details,params:[{name,description}],returns,notes}`, relationships `{uses,used_by,ambiguous_targets}`, generation provenance and explanation status. Strings rendered as untrusted text/allowlisted Markdown. File response provides source hash, exact selected line slice, total count, link spans and notes. Ranges refer exclusively to requested build, never latest branch.

## Server-sent events

Event ID is monotonically increasing `build_events.sequence` scoped to build. Types: `build.queued`, `stage.started`, `task.progress`, `usage.updated`, `stage.completed`, `build.ready`, `build.failed`, `build.cancelled`, `build.budget_exhausted`. Payload contains build_id, stage, completed/total counts, safe reason, updated_at. Heartbeat every 15s; reconnect backoff capped 30s; Last-Event-ID replays retained events. Expired cursor yields `reset` with snapshot URL. Permission rechecked at least every 30s and before each sensitive event. Fallback GET build status every 5s with jitter. Limit 3 streams/session and 20/workspace initially.

## Quotas and webhooks

Initial rate policies: authenticated API 120/min/user; sign-in starts 10/min/IP; estimate/acquire 10/hour/workspace; build starts 20/day/workspace or lower plan limit; exports 10/hour/workspace; public delivery 300/min/IP with bounded response size. Database atomic counters or gateway controls must work across replicas; no per-process-only enforcement. Operator overrides are audited.

GitHub validates `X-Hub-Signature-256` over raw bytes in constant-time, delivery ID unique, body max 2 MiB. MVP handles installation/installation_repositories revocation and repository rename/transfer; push rebuild P1. Return success only after receipt persisted. Stripe uses SDK signature verification, event dedupe and fetching latest subscription state when event ordering is ambiguous. Both handlers reject malformed or unknown tenant associations and do not trust webhook-supplied workspace IDs.
