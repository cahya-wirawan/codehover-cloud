# Test and evaluation plan

Use pytest/pytest-asyncio for domain and integration tests, actual PostgreSQL 18 and RabbitMQ containers for concurrency semantics, Vitest/React Testing Library for components, Playwright for E2E, axe plus manual screen-reader/keyboard review for accessibility. Replay model/OAuth/GitHub/billing adapters provide deterministic fixtures; a separately opted-in paid-provider smoke suite has a hard cost cap. No live private repository is required in CI.

| Layer | Essential scenarios | Gate |
|---|---|---|
| unit | path normalization; parser spans; cache dependency digest; currency rounding; retry classification; prompt/schema validation | Deterministic outputs and boundary coverage |
| DB integration | uuidv7 defaults; composite FKs/RLS under runtime role; rollback isolation; Alembic fresh/upgrade | No cross-tenant reads/writes; UUID version 7 |
| queue integration | crash after outbox publish; duplicate message; lease expiry/fencing; dead letter; missing notification reconciliation | One terminal result and idempotent ledger settlement |
| budget concurrency | simultaneous tasks reserve last credits; timeout unknown spend; cancellation during call; renewal rollover | No overspend beyond reserved maxima; no double charge |
| auth integration | forged/replayed state; OIDC issuer/audience/nonce; linking collision; unverified email invite; final owner race | Reject every invalid grant |
| GitHub integration | revoked install; stale ref; renamed/transferred repo; spoofed install ID; forbidden repository | No unauthorized snapshot or publication |
| API integration | every endpoint with wrong role, wrong tenant, deleted object; validation/idempotency/ETag/pagination | Consistent envelope and no existence leak |
| AI eval | 50+ definitions/30+ claims; incorrect citations; injection; ambiguous links; absent checker; malformed JSON | 100% deterministic span validity; report semantic precision/recall/CI |
| viewer UI | tree, pinned card, used-by, source ranges, search, guide citations, skipped coverage | Exact build and navigation semantics |
| security | tar traversal/symlinks/bomb; XSS source/Markdown/Mermaid; SSRF redirects; token/body log leakage | No exploit in corpus; resource cap enforced |
| billing | duplicate/out-of-order/forged webhook; checkout return without payment; dispute/refund/reconcile | Entitlements only from validated authoritative state |
| offline/network | SSE disconnect/cursor reset/polling; expired session mid-view; network loss before publish | No replayed write; clear stale/offline state |
| export | ZIP offline local HTTP; no CDN; safe filenames; KB schema round-trip; authorization at download | Complete selected source/annotations only |
| performance | 20k-symbol search fixture; 50 concurrent viewers; 2 builds/tenant contention; big source scrolling | NFR p95 targets and bounded memory |
| recovery | PG PITR/object backup restore; deletion tombstones reapplied; lost RabbitMQ notifications | RPO/RTO objective and no source resurrection |

Critical E2E journey: Google or GitHub mock sign-in → workspace → verified install/repo → snapshot and exclusion preview → estimate → queued replay build → read code/guide → review → private publish → second member reads → unauthorized tenant blocked → export → public confirm/unpublish → delete and verify delivery denied. M3 automates this journey in `frontend/e2e/journey.spec.ts` (Playwright with axe: serious or critical WCAG 2.1 A/AA findings on any screen fail it) against a test-only server (`backend/tests/e2e/server.py`): the real API on the integration database with fake Google and GitHub providers, an inline worker on the replay preset, and every request outside the app aborted. The GitHub connection is seeded, since the install flow has its own API tests. The second member joins through an invitation; another signed-in tenant and an anonymous reader get "Site not found"; the export is read in a context that blocks every network request. M4 adds the delete step: the project is published privately again, read by the member, deleted with the typed name, and the member then gets "Site not found" while the project and build APIs return 404. `tests/integration/test_deletion.py` covers purge, retry after a failed purge, workspace and account deletion, and tombstone replay after a simulated restore. Repeat real OAuth callback smoke in staging with dedicated test accounts; mock tests alone are insufficient for production setup.

Language fixtures cover Python/TS/JS and Unicode columns, CRLF, surrogate pairs, nested functions, decorators, overloads and syntax errors. A byte/Unicode/UTF-16 mismatch must not point hover at the wrong symbol. Accuracy eval labels distinguish actual code behavior, plausible inference and unsupported prose. Human rating instructions and disagreement adjudication must be recorded. Test results belong to future implementation; package structural validation is not product validation.
