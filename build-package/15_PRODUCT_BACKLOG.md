# Actionable product backlog

Each task needs implementation, focused verification and updated contracts; estimates are relative effort S/M/L, not promised dates. P0 is the launch-critical set, with paid gate explicitly separated.

| Task | Priority / effort | Concrete deliverable | Depends on | Done when |
|---|---|---|---|---|
| T01 | P0/M | Create monorepo, Python/Node locks, lint/type/build CI | — | Both applications compile and health smoke passes |
| T02 | P0/L | Alembic identity/workspace/project/build/job/ledger tables, uuidv7, RLS, runtime/system/resolver roles and lookup functions | T01 | Fresh/upgrade and wrong-tenant tests pass; `contracts/core_schema_checks.sql` assertions ported to CI |
| T03 | P0/L | Google/GitHub callbacks, SQL sessions, CSRF, explicit link/unlink | T02 | Callback replay/link collision/security tests pass |
| T04 | P0/M | Workspace/member/invitation policy including verified email and final owner | T03 | Role matrix and concurrent removal tests pass |
| T05 | P0/L | GitHub App connect/installation association (App user-token verification) and repo/ref listing | T03–04 | Spoofed install, other user's installation and forbidden repo denied |
| T06 | P0/L | Immutable archive acquisition, bounded extraction, secret exclusion/scan | T05 | Traversal/link/bomb/input-size fixtures blocked |
| T07 | P0/L | SQL tasks/outbox, publisher confirms, worker lease/fencing/reconciler | T02 | Publish/ack/crash fault suite passes |
| T08 | P0/L | Versioned engine interfaces, sandboxed parse runner, parse/rank and deterministic replay artifacts | T06–07 | SHA/span/source manifest matches fixture; parser network-isolation and no-credential tests pass |
| T09 | P0/L | Private tree/file/symbol/guide endpoints and minimal viewer | T08 | Wrong tenant blocked for every payload/asset |
| T10 | P0/L | Catalog, estimate expiry/config hashes, reservation and settlement ledger | T02,08 | Racing reservations cannot exceed cap |
| T11 | P0/L | Provider gateway, strict explanations/guide generation, call retry and provenance | T10 | Schema/injection/cost fixtures pass |
| T12 | P0/L | Deterministic citations, independent checker, repair and review gate | T11 | Unchecked/invalid never labeled supported |
| T13 | P0/M | Build UI, SSE resume/poll fallback, cancel/retry/status | T07,09–12 | Network/cancel/crash E2E passes |
| T14 | P0/L | Refined code/guide UX, accessible popovers, relation navigation, PostgreSQL search | T09,12 | Keyboard/touch/Unicode/search fixtures pass |
| T15 | P0/M | Publication pointer, private/public confirmation, rollback/suspension/revocation | T12,14 | Atomic swap and public removal tests pass |
| T16 | P0/M | Static ZIP and versioned KB export, offline assets, download auth | T14 | Network-blocked export and authorization pass |
| T17 | P0/M | Audit, retention/purge/tombstones and user export/delete | T04,15–16 | Deletion blocks immediately and purges within target |
| T18 | P0/M | Metrics/traces, safe diagnostics, health/readiness and operator view | T07–17 | Sensitive data absent from telemetry; alerts exercised |
| T19 | P0/L | Deployment images, migrations job, production secret/TLS/backup guide | T01–18 | Staging restore/load/security gates pass |
| T20 | P0 paid/L | Stripe checkout/portal/webhooks and reconciliation/plan lifecycle | T10,18–19 | Out-of-order/forgery/dispute matrix passes |
| T21 | P1/M | GitHub push dedupe/debounce and branch freshness | T15 | Repeated push schedules one appropriate build, never public auto-publish |
| T22 | P1/L | German/Indonesian and beginner/expert variants | T12,16 | Citation markers/schema and variant anchor tests pass |
| T23 | P1/L | Sandboxed Python/TS/Rust SCIP integration | T06,08 | No network/install/hooks; timeout fallback labeled |
| T24 | P1/L | Batch and BYOK adapters | T10–12 | Persisted batch IDs/keys, no silent price fallback |
| T25 | P2/L | Authenticated hosted MCP (done: `/mcp/{slug}` with personal access tokens, 0035) and custom domain delivery | T15–19 | Scoped tokens/origin/cross-tenant evals pass |

Engineering definition of done: current contract; no unreviewed tenancy path; migration included for schema changes; focused tests at the relevant trust boundary; sanitized error handling; user-visible states; no unexpected provider calls in tests; docs and metrics updated. Maintain requirement/test traceability through FR IDs and acceptance scenarios.
