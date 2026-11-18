# Package validation

Date: 2026-11-18 · Package version 0.5.4

0.5.4 adds the M2 evaluation baseline. On the corpus, the replay run passes every hard gate:
- scan as labeled;
- 50 of 50 labeled definitions matched (precision and recall 1.0) and 100% span validity;
- the malformed file reported as a parse error;
- 9 of 9 explanation answers valid, none containing injected text, and every prompt with intact data blocks;
- 4 of 4 invalid citations rejected with the labeled reason.

On 34 labeled claims, the replay checker's supported-label precision is 0.54 (Wilson 95% 0.35–0.71), and recall 0.67 (0.45–0.83). That measures a heuristic and is the reason a preset may no longer pair a model writer with it. The labels are drafts by the implementing assistant, and the report marks the baseline provisional until two maintainers label it; no accuracy claim follows from it. A model-checker baseline needs an operator catalog, an API key and approval to spend. CI pins the baseline report and checks that it reproduces byte for byte. 263 backend tests pass.

0.5.3 records cancellation, retry and event polling. Integration tests cover:
- a queued build cancelled at once with its full envelope released;
- a cancel during a call: in-flight calls settle, no later stage runs, and the ledger balances (A10);
- with one call in flight, the next unit refused at its earmark, so exactly one call happens;
- a retry after a checker outage that pays only for checks and becomes publishable (A13);
- retry rules: not while running, not with another snapshot's estimate, and 404 for strangers;
- ordered, resumable events.

Removing the cancel check at the earmark, or the cancel at the stage boundary, is caught. The frontend adds the estimate flow (snapshot, scope, preset, cost range, price revision, expiry, cap), cancel and retry, usage, and the guide with claim badges. Guide text renders as text, and a test confirms that an HTML-looking model output creates no element. 259 backend and 10 frontend tests pass.

0.5.2 records T12. Integration tests cover:
- a replay guide whose claims are all supported, so the build is publishable;
- a checker outage that leaves every claim unchecked on a ready draft and blocks publication (A13);
- a repair cycle that removes unsupported claims, keeps the original page and rows, and re-checks only changed claims;
- unsupported claims that block until an admin waives them with an audited reason, then stay counted;
- a citation by path instead of file ID that is invalid, never sent to the checker, and refused for waiver by the API and by a database CHECK (A14);
- quality endpoints that are private to the workspace.

Unit tests cover extraction (trailing citations, list items, code fences, slug-bound keys), each deterministic rejection reason, checker and repair validation, and the gate. Seven mutations are caught: citing excluded files, skipping range bounds, treating an outage as supported, waiving anything, letting unchecked claims through, sending invalid claims to the checker, and keeping superseded rows current. 253 backend tests pass.

0.5.1 fixes two findings of the automated review of T11. With the scope narrowed to `src/app.py`, no captured prompt contains `src/util.py`, its `helper` definition, or the excluded secrets; removing the new filter makes that test fail. New allowlist cases reject reference-style and autolink `javascript:` targets and raw HTML in list items and blocks. 232 backend tests pass.

0.5.0 records T11, the provider gateway and generation stages. Integration tests run the real pipeline with the deterministic replay gateway, and a scripted fake stands in for a priced provider; no real provider is called. They cover:
- a free replay-preset build through explain, overview, plan, guide and render, with labeled cards, file explanations, cited guide pages, a manifest listing the guide, and the guide endpoints private to the workspace;
- priced calls whose actual cost never exceeds the earmark, with a ledger that balances (reserve, settle, release) and provider request IDs kept;
- budget exhaustion before any request;
- a rate-limited call retried without charge, and an invalid answer billed and then repaired;
- a refusal falling back to a labeled source-only card;
- an unknown outcome keeping its hold after the build ends;
- a crash after paid output resuming from the checkpoint without paying again (A09);
- a fenced-out worker settling and checkpointing nothing;
- a killed worker's attempts becoming `unknown` on re-claim, with their holds kept.

Unit tests cover chunking, data-boundary markers, validators, the Markdown allowlist, the schema projection, and the Anthropic adapter against a mock transport, including error normalization and a request with no sampling parameters or tools. Mutation checks catch removing the earmark budget check, the settlement fence, unknown-on-re-claim, cache reuse, the repair call, symbol-key validation and the raw-HTML check. Cancellation has no test until T13. 224 backend tests pass (one Linux-only test skipped on macOS).

0.4.2 bounds scope selection. Against 2000 files with 29 segments of 200 characters each and 40 hostile but valid globs, every selection finishes or stops at the budget within 1.6 s (previously 61 s); ordinary globs take 0.2 s on those paths and 37 ms on a realistic repository. 183 backend tests pass.

0.4.1 fixes two findings of the automated review of T10. A probe of `PurePath.full_match` with `*a*a*a*a*a*a*a*a*a*a*b` against a 960-character path did not finish in two minutes; the new matcher agrees with it on ordinary globs and handles the hostile ones in milliseconds (unit tests). An integration test shows a second project's acquisition refused while the first is pending, and admitted after it finishes. 180 backend tests pass (one Linux-only test skipped on macOS).

0.4.0 records T10 (M2): snapshots before builds, estimates and single-envelope reservations. Integration tests on PostgreSQL 18 cover:
- a snapshot acquired and scanned before any build, with signed, filter-bound file cursors;
- an immutable estimate that expires, goes stale when the catalog changes, and fixes the approved commit even after the branch moves (A05);
- a cap reserved once and released at the end, with ledger entries balancing the account counters;
- a cap above the remaining allowance refused, and the workspace active-build limit;
- a two-session race in which the second reservation waits for the account lock (A08);
- other tenants seeing 404 for estimates, snapshots and usage.

Mutation checks catch removing the account row lock, the allowance check, the expiry check and the terminal release. An API-level race alone did not catch the missing lock, because the two requests ran one after the other; the direct two-session test does. 176 backend tests pass.

0.3.12 hardens the sandbox. The Linux probe ran with a world-readable `/run/secrets/github_app_key` present. Inside the sandbox it could not read the parent's environ, `/etc` or the secret, and could not exec or open any socket; the parser still works under Landlock. On the Compose stack, the hardened worker passed the stricter self-check, and its own environ is unreadable to same-UID processes (`PR_SET_DUMPABLE=0`). 146 backend tests pass.

0.3.11 records the M1 implementation. M1 exit criteria are met by these tests:
- **End-to-end build:** an immutable fixture build runs from a GitHub ref through acquire, index, explain and render to the private viewer, with secrets excluded or blocked, Unicode columns, name-based references and a manifest stored last.
- **Cross-tenant isolation:** tenancy, flat-route 404s and the viewer are private.
- **Crash and retry:** duplicate notifications claim once; a lost lease is fenced and reconciled; transient failures retry, then fail the build; cancelled builds are not claimed; lost messages are re-notified; dispatch backs off on broker failure.
- **Parser network isolation:** on Linux, inside the sandbox, IPv4, IPv6 and Unix socket creation are refused, and the environment is empty.

145 backend tests pass on PostgreSQL 18 (one Linux-only test runs in a Linux container and CI), plus 8 frontend tests. Mutation checks were caught for completion fencing, live-lease re-claim, cancelled-build claims, and every earlier guard. On the running Compose stack:
- the hardened worker passed its sandbox self-check;
- tasks flowed outbox, dispatcher, RabbitMQ, worker;
- a poison message was refused and kept in `codehover.dead`.

0.3.10: migration 0004 fails closed. A migration test creates a repository at 0003, upgrades, and asserts that it is revoked; the test fails if the fail-closed step is removed. 96 backend tests pass.

0.3.9 limits GitHub connections to the connector's repository grant (09). Two integration tests cover a member who can read one of three repositories, and a narrower reconnect revoking the rest. Two mutations, granting the whole installation at connect and listing without the grant, are each caught by both tests. 95 backend tests pass, and migration 0004 runs in the Compose `migrate` job.

0.3.8 records the T05 GitHub App flow. The implementation passes 10 GitHub integration tests on PostgreSQL 18 against a fake GitHub. The fake checks PKCE, the App JWT signature and issuer, and per-installation repository access. The tests cover spoofed and other users' installations, a different GitHub account, a Google-only user, phase confusion, other-browser and other-session hijacking, mid-flow demotion, a repository removed from the installation, and a revoked connection. Five code mutations of the guards are each caught. 93 backend tests pass.

0.3.7 records T04 workspace membership. The implementation (migration 0003) passes 10 workspace integration tests on PostgreSQL 18, part of 83 backend tests. They cover the role matrix, invitations with verified-email matching, single use, revocation and expiry, non-member 404s, ETag preconditions, a signed and scoped pagination cursor, and a true two-connection race on the last two owners, of which exactly one commits. Mutation checks:
- The admin role bound, the invitation email check and the member-list workspace filter are each caught.
- Dropping the owner trigger is caught by the race test.
- Removing the application's last-owner check is equivalent, because the trigger still refuses with `LAST_OWNER`.

0.3.6 binds OAuth flows to the starting browser (09). The login-CSRF integration test opens a callback in a browser without the flow cookie, then with a forged cookie, and confirms that both are refused and that the originating browser can still complete. A schema mutation that ignores the binding is caught by two tests. Removing the application's missing-cookie check is an equivalent change, because the database guard still refuses; the check stays as a second layer. 73 backend tests pass.

0.3.5 records sign-in decisions implemented in T03. They are exercised by 26 sign-in integration tests on PostgreSQL 18 with mocked Google and GitHub providers, which sign real RS256 ID tokens and check PKCE. Nine code mutations of security checks were applied; eight were caught. The ninth, dropping the explicit RS256 allowlist, is equivalent, because joserfc's defaults already reject `alg: none` and HS256 algorithm-confusion tokens; the allowlist stays as defense in depth. The Docker gateway was also checked: signed-out 401 with `no-store`, and unconfigured providers redirect to the login page.

0.3.4 adds `idempotency_keys` and the pre-authentication lookups to 05. Both are implemented by application migration 0002, which was:
- executed on PostgreSQL 18 (upgrade, downgrade and re-upgrade, also inside the Compose `migrate` job);
- covered by 46 backend tests, including 21 integration tests for tenancy, identity, sessions, OAuth state, append-only tables, the system role and integrity rules;
- mutation-tested: six injected privilege or policy defects were each caught.

0.3.3 adds the `SPEC_VERSION` conformance link (23). Both images were rebuilt with the new `SPEC_VERSION` build argument; both carry `codehover.spec.version=0.3.3` next to `org.opencontainers.image.version=0.1.0`, and the stack restarted ready. `scripts/check_versions.py` rejects an unknown, non-SemVer or newer-than-package `SPEC_VERSION`, and reports, without failing, an implementation behind the spec. Everything below was executed for 0.3.2 and is unaffected.

0.3.2 is the first version whose container templates (`deployment/`) were executed. They ran against the M0 implementation in the repository root. The steps were:
1. Built both images.
2. Started the full Compose stack.
3. Exercised it through the gateway and from a real browser (details below).

The SQL contracts are unchanged since 0.2.0, and the versioning tooling since 0.3.0. Earlier labels were renumbered: "1.0" is 0.1.0, and "1.1" (briefly "2.0.0") is 0.2.0.

## Container templates executed (0.3.2)

Docker 29.8.2, Compose 5.5.1, linux/x86_64, `--profile app`:
- **Images.**
  - Both images build from the hash-pinned Python lock and the npm lockfile.
  - The frontend bundle is byte-identical to a local build.
  - The backend image contains no tests, `.env` files or keys; its code is root-owned and not writable by the runtime user (uid 10001); its OCI version label is 0.1.0.
- **Start-up order.**
  1. `db`, `rabbitmq` and `objectstore` (SeaweedFS 4.52, pinned by digest).
  2. `migrate`, which exits 0.
  3. `bootstrap`, which exits 0.
  4. `api` (healthy), the worker shells, and `web`.

  `/api/v1/health/ready` is ready through the gateway. Re-running `migrate` and `bootstrap` is a no-op.
- **Gateway.**
  - `/`, deep links, hashed `/assets/` and `/api/` responses all carry nosniff, Referrer-Policy, X-Frame-Options and the CSP. The assets location fix from 0.2.0 was confirmed.
  - The SPA shell is `no-cache` and assets are `immutable`.
  - nginx's request ID reaches API responses.
- **Browser** (Chrome). The app renders and fetches `/api/v1/health/live` with no console errors or CSP violations. Unknown routes render the in-app not-found view.
- **Object store.** Bucket `codehover` exists. Anonymous list, read and PUT return 403.
- **Broker.** The durable topic exchange `codehover.dlx` exists. Policy `codehover-work-dlx` applies to `^codehover\.work\.` quorum queues with the DLX and `delivery-limit` 20.
- **Database roles.**
  - The API connects as `app_runtime`.
  - `app_runtime`, `app_system` and `app_resolver` exist, none superuser or BYPASSRLS.
  - The application's integration tests pass against the container database: migration down/up cycle, readiness, and role limits.
- **Shutdown.** The worker, dispatcher and scheduler exit 0 on SIGTERM.
- **Found and fixed during the run.**
  - The default host port 5432 collided with a local PostgreSQL; the host-port overrides were added.
  - A bootstrap log field used a reserved `LogRecord` key and crashed the job; it was fixed in the application, with a regression test.

## Versioning checks (0.3.0)

`tools/assemble.py --check` now also verifies:
- `VERSION` is valid SemVer 2.0.0 and matches the newest changelog release;
- the changelog has an Unreleased section, unique releases, and descending SemVer precedence and dates;
- README, master spec and manifest carry the stamped version;
- every `contracts/*.schema.json` has a SemVer `$id` URN whose MAJOR matches its title and any `schema_version` const.

The following were exercised on a scratch copy:
- `bump minor`;
- refusal to bump an empty Unreleased section;
- rejection of a non-SemVer `VERSION`;
- detection of an out-of-order changelog;
- detection of an `$id`/`schema_version` mismatch.

SemVer precedence sorting was checked against the semver.org example ordering.

## Executed in 0.2.0

- **SQL contracts on PostgreSQL 18.0** (local throwaway cluster). Steps:
  1. Ran `deployment/init-db.sh` with the compose environment variables.
  2. Ran `contracts/core_schema.sql` as the bootstrap/DDL user.
  3. Ran `contracts/core_schema_checks.sql`.

  All checks C01–C09 passed:
  - UUIDv7 defaults;
  - no rows without a tenant context;
  - tenant isolation and rejected cross-tenant writes;
  - user-scoped workspace listing;
  - the resolver functions;
  - atomic workspace creation;
  - the append-only ledger;
  - the `app_system` scope, which can claim due work across tenants but cannot read tenant tables or task outputs;
  - one active build per project, same-project publication FK, public-slug CHECK and budget cap CHECK.
- **Mutation test of the checks.** Six deliberate defects were each applied inside a rolled-back transaction, and each was caught by its check:
  - a permissive runtime policy;
  - a system role able to read projects;
  - an UPDATE grant on the ledger;
  - the dropped one-active-build index;
  - the dropped user-membership policy;
  - the dropped public-slug CHECK.
- **`python3 tools/assemble.py --check`**:
  - every JSON file parses and every `*.schema.json` is a valid Draft 2020-12 schema;
  - YAML parses, including Compose merge keys and service dependencies;
  - shell syntax passes;
  - Markdown code fences are balanced and there are no TODO/TBD markers;
  - the master spec matches its sources;
  - the manifest checksums match.
- **Explanation schema v2.** A sample document validates.
- **Design tokens.** WCAG contrast ratios were computed for every text token and the new `input-border` token (results in 17).

## Corrected from 0.1.0

The 0.1.0 report listed "document checksum consistency" as passed, but its manifest was stale for 7 files and the master spec. The master spec itself matched its sources. Checksums are now generated by `tools/assemble.py` rather than maintained by hand.

## Not performed

- `nginx -t` as a separate step (the config was loaded and exercised by the running gateway instead). Multi-node RabbitMQ, managed object storage and production TLS ingress.
- SQL beyond the core subset and the M0 baseline migration.
- Live OAuth, GitHub App installation, provider calls and billing.
- Accessibility audits, performance, security penetration tests, and end-to-end tests beyond the M0 shell.
- The GitHub App installation-verification flow (09) relies on GitHub's documented `GET /user/installations` behavior and must be confirmed against a real App in M1.

The core SQL is a subset design reference. The API specification is a design contract, not generated OpenAPI. Container templates require the future application files and exact dependency lockfiles. Production image tags and digests, and the model/provider configuration, remain implementation gates.
