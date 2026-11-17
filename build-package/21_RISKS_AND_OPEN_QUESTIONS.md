# Risks, assumptions and decisions

## Risk register

| Risk | Impact / likelihood | Mitigation / owner |
|---|---|---|
| Private source exposed through public export/site/cache | Critical / medium | Private default, explicit disclosure, authorization of all derivatives, origin separation; security lead |
| Malicious repository triggers code execution | Critical / medium | No project execution/install, no-network parsers, strict acquisition; backend lead |
| Duplicate work or uncertain model charges | High / high | Fenced task attempts, reservation ledger, reconciliation; backend lead |
| Plausible but incorrect explanations/checker agreement | High / high | Independent evidence checks, source-first UI, labeled evals and visible uncertainty; AI lead |
| Broad workspace grants surprise users | High / medium | Explicit all-project access disclosure, separate workspaces, P1 per-project ACL; product lead |
| Language parser/column mismatch | High / medium | Unicode/UTF-16 conversion fixtures, supported-language gates; frontend/engine leads |
| Incomplete reference-context cache invalidation | High / medium | Context dependency digest or conservative invalidation; engine lead |
| Subscription margin too low | High / medium | Provider-cost ledger, quotas, no overage, measured price validation; product owner |
| Revoked repository authority and retained docs | High / medium | Suspend affected private publications and require owner review; product/security |
| Public withdrawal cannot erase external copies | High / certain after download | Explicit disclosure, no recall guarantee; product/legal review |
| Provider region/retention unsuitable | High / medium | Approved catalog/procurement policy and consent; operator |
| Compose mistaken for production HA | Medium / medium | Local templates labeled; managed infrastructure/restore gate; operations |
| Local object store image unmaintained (MinIO community archived) | Medium / certain | Choose maintained S3-compatible image in M0; app depends on S3 API only; managed storage in production; operations |
| Installation linked by someone without access to it | Critical / medium | GitHub App user-token check against `GET /user/installations` plus linked-identity match; acceptance A03; security lead |

## Defaults selected for implementation

English standard output first; Python/TS/JS parser fixtures first; GitHub App read-only ingestion; one approved hosted model + replay; membership-only private sharing; all workspace projects shared with all members; PostgreSQL lexical search; no runtime LLM reads; manual beta entitlements; Stripe at paid launch; no automatic paid overage; controlled React hosted viewer; self-contained static ZIP export. These let implementation proceed without basic product decisions.

## Open decisions and timing

| Decision | Default / explicit uncertainty | Required before |
|---|---|---|
| Provider/model/preset and data region | Choose approved catalog via evals, not reference model name assumptions | M2 real private-source call |
| Cloud/region/domain and S3 provider | EU region preferred hypothesis; local S3-compatible store for dev (MinIO replacement to choose) | M0 for local image; production deployment for provider |
| Reuse codehover engine vs independent modules | Independent implementation default; reuse only after license/API/security review | Engine implementation scope change |
| Market plans/pricing/allowances | Proposed trial/Pro/Team; validate economics/customer need | Paid launch |
| Private site retention after repo access loss | Suspend access immediately; owner reconnect/review; no silent deletion | Beta terms |
| Model output licensing and data processing contracts | Procurement/legal review pending | Public paid launch |
| Need for per-project ACL | Workspace-wide MVP disclosed; use separate workspaces | Multi-client/team beta feedback |
| Output variants and SCIP cost | P1 gated by demand/sandbox/evals | P1 feature rollout |

No user answer is needed to read/use this blueprint. The listed production credentials, contracts and model choices are deployment gates, not missing basic architecture. The reference repository was not executed; generated capabilities were established from source inspection only.
