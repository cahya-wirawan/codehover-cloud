# Critical acceptance criteria

| Scenario | Given / When / Then | Requirements |
|---|---|---|
| A01 Sign-in | Given a valid one-use provider transaction, When callback identity is validated, Then rotate a server session and redirect only to an allowed relative path | FR-001 |
| A02 Linking | Given matching email but different unlinked subjects, When another provider signs in, Then do not merge accounts automatically | FR-001 |
| A03 Installation | Given a forged installation ID, or a valid installation the linked GitHub user cannot access, When setup callback is submitted, Then reject despite a valid state and repository name | FR-003,024 |
| A04 Tenant isolation | Given users in unrelated workspaces, When IDs/objects/search/export are substituted, Then no content, counts or access is exposed; the database checks in `contracts/core_schema_checks.sql` pass under the runtime and system roles | FR-002,024 |
| A05 Snapshot | Given tracked branch moves after estimate, When generation starts, Then use the approved immutable SHA or require a new estimate, never silently use latest | FR-004,007–008 |
| A06 Archive | Given links/traversal/oversize archive, When acquisition runs, Then stop before extraction/provider calls and persist safe rejection | FR-005,024 |
| A07 Scope | Given excluded credential file, When any prompt/artifact/search/export is created, Then its contents remain absent | FR-005–006,024 |
| A08 Budget | Given parallel requests for final available credits, When tasks reserve, Then at most affordable requests proceed and ledger balances stay consistent | FR-008,020 |
| A09 Retry | Given crash after provider output but before ack, When task redelivers, Then existing result/settlement is reused or unknown outcome reconciled without silent double billing | FR-009,020 |
| A10 Cancellation | Given an in-flight model call, When user cancels, Then no new call starts, completed usage settles, reservation remainder releases and no publication occurs | FR-009 |
| A11 Hovers | Given a definition/reference/parameter, When focus/tap/hover opens its card, Then correct snapshot evidence appears and Escape returns focus | FR-010,013 |
| A12 Ambiguity | Given name matching cannot identify unique target, When shown, Then candidates and name-match label are visible without false type-resolution claim | FR-010,015 |
| A13 Checker | Given checker timeout or malformed answer, When quality report is computed, Then status is unchecked, publish is blocked, and a retry build re-runs the checks while reusing cached generation for the same snapshot/config | FR-009,012,016 |
| A14 Citation | Given invalid path/range or mismatching snippet, When validation runs, Then invalid status blocks publication and cannot be waived | FR-012,016 |
| A15 Publication | Given private source, When an editor finishes a build, Then it stays private until admin review; public requires explicit disclosure confirmation | FR-016–017 |
| A16 Atomic update | Given build A published and B building, When B fails, Then A continues serving; switching to ready B is atomic and version checked | FR-016–017 |
| A17 Revocation | Given member removed/installation revoked, When subsequent access/build/publication occurs, Then authorization/policy blocks it and streams close within 30s | FR-017,027 |
| A18 Search | Given exact identifier and context query fixtures, When searched, Then authorized current-build results rank correctly and unresolved relations remain labeled | FR-014 |
| A19 Export | Given authorized export, When downloaded and opened offline, Then selected source/cards/guide/local assets work and no excluded/private-other-tenant files exist | FR-018 |
| A20 Billing | Given checkout redirect but missing payment webhook, When UI returns, Then no paid entitlement is granted until authoritative reconciliation | FR-022 |
| A21 Offline | Given network lost before Publish, When connection returns, Then no write replays automatically and current state is fetched before action | FR-017 |
| A22 Deletion | Given project deletion, When requested, Then serving stops immediately, tasks are fenced, objects purge ≤24h and restored backups honor tombstone | FR-021 |
| A23 Freshness | Given provider unavailable, When checking latest branch, Then show unknown with last-observed time rather than claiming current | FR-019 |
| A24 Telemetry | Given private source and provider credentials, When errors/traces/events emit, Then no code/token/query/path leaks into telemetry | FR-023–024 |

“Correctly” for search and generated explanation means measured against checked fixtures/human labels in the test plan, not assumed from UI appearance. Acceptance must be recorded per implementation release with evidence.
