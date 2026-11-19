# Risks, assumptions and decisions

## Risk register

| Risk | Impact / likelihood | Mitigation / owner |
|---|---|---|
| Private source exposed through public export/site/cache | Critical / medium | Private default, explicit disclosure, authorization of all derivatives, origin separation; security lead |
| Malicious repository triggers code execution | Critical / medium | No project execution/install, no-network parsers, strict acquisition; backend lead |
| Duplicate work or uncertain model charges | High / high | Fenced task attempts, reservation ledger, reconciliation; backend lead |
| Plausible but incorrect explanations/checker agreement | High / high | Independent evidence checks, source-first UI, labeled evals and visible uncertainty; AI lead |
| Broad workspace grants surprise users | High / medium | Explicit all-project access disclosure, separate workspaces, per-project access (restricted projects, 0027); product lead |
| Language parser/column mismatch | High / medium | Unicode/UTF-16 conversion fixtures, supported-language gates; frontend/engine leads |
| Incomplete reference-context cache invalidation | High / medium | Context dependency digest or conservative invalidation; engine lead |
| Subscription margin too low | High / medium | Provider-cost ledger, quotas, no overage, measured price validation; product owner |
| Revoked repository authority and retained docs | High / medium | Suspend affected private publications and require owner review; product/security |
| Public withdrawal cannot erase external copies | High / certain after download | Explicit disclosure, no recall guarantee; product/legal review |
| Provider region/retention unsuitable | High / medium | Approved catalog/procurement policy and consent; operator |
| Compose mistaken for production HA | Medium / medium | Local templates labeled; managed infrastructure/restore gate; operations |
| Local object store image unmaintained (MinIO community archived) | Resolved in M0 | SeaweedFS pinned by digest for local/CI; app depends on S3 API only; managed storage in production; operations |
| Installation linked by someone without access to it | Critical / medium | GitHub App user-token check against `GET /user/installations` plus linked-identity match; acceptance A03; security lead |

## Defaults selected for implementation

English standard output first; Python/TS/JS parser fixtures first; GitHub App read-only ingestion; one approved hosted model + replay; membership-only private sharing; all workspace projects shared with all members; PostgreSQL lexical search; no runtime LLM reads; manual beta entitlements; Stripe at paid launch; no automatic paid overage; controlled React hosted viewer; self-contained static ZIP export. These let implementation proceed without basic product decisions.

## Open decisions and timing

| Decision | Default / explicit uncertainty | Required before |
|---|---|---|
| Provider/model/preset and data region | Choose approved catalog via evals, not reference model name assumptions | M2 real private-source call |
| Cloud/region/domain and S3 provider | EU region preferred hypothesis; local/CI store decided: SeaweedFS | Production deployment |
| Reuse codehover engine vs independent modules | Independent implementation default; reuse only after license/API/security review | Engine implementation scope change |
| Market plans/pricing/allowances | Proposed trial/Pro/Team; validate economics/customer need | Paid launch |
| Private site retention after repo access loss | Suspend access immediately; owner reconnect/review; no silent deletion | Beta terms |
| Model output licensing and data processing contracts | Procurement/legal review pending | Public paid launch |
| Need for per-project ACL | Resolved: restricted projects with viewer or editor grants (0027). Per-project roles above editor (publishing) stay workspace admin powers | Multi-client/team beta feedback |
| Output variants and SCIP cost | Variants shipped in M6 (FR-026). SCIP (T23) shipped in 0.5.27 after its review. Dependencies: scip-python 0.6.8 (MIT) and scip-typescript 0.6.0 (Apache-2.0), 64 packages in all under MIT, ISC, Apache-2.0 and BSD-3-Clause, installed from an integrity-pinned lockfile with install scripts off; none needs one. With Node 24.22.3 they add 198 MB to the backend image. They run in the sandbox's indexer profile (22), and a timeout or failure falls back to name matching, labeled as such. Open: a call through an interface or Protocol links to the interface method, not its implementations; the repository's own tsconfig (path aliases) and Python settings are never read, so such imports stay unresolved; one indexer run per language family adds about a minute for 200 files | Product lead and security review |
| Publishing sites of third-party public repositories | Public repositories (FR-028) shipped in 0.5.28. Builds are private by default, and making a site public keeps the typed confirmation, but nothing yet checks that the publisher may redistribute someone else's code and explanations of it. Options: allow it only when the repository's license permits redistribution, require an acknowledgement, or refuse public sites for repositories the workspace does not own (an App grant). The takedown process (22) covers abuse meanwhile. Also open: refresh is manual (no webhooks), and the shared anonymous rate limit needs `GITHUB_PUBLIC_TOKEN` for more than light use | Product lead and legal review |

No user answer is needed to read/use this blueprint. The listed production credentials, contracts and model choices are deployment gates, not missing basic architecture. The reference repository was not executed; generated capabilities were established from source inspection only.
