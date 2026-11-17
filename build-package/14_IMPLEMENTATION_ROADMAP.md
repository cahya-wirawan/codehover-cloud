# Implementation roadmap

Milestones are dependency ordered; no calendar estimates are assumed.

| Milestone | Deliverables | Dependencies | Exit criteria |
|---|---|---|---|
| M0 Foundation | Monorepo, pinned toolchains/locks, FastAPI/Vite shell, migrations, infrastructure Compose, CI | Approved package defaults | Clean build/start; PG18 migration; no secret in frontend |
| M1 Private replay vertical slice | OAuth/session, membership/RLS, GitHub App verification/acquisition, scan, outbox/Rabbit worker, replay engine, private viewer | M0 | One immutable fixture build end to end; cross-tenant and crash/retry tests pass |
| M2 Grounded generation | Catalog/provider gateway, preflight estimate/reservations, explanations/guides/checker, budget/cancel/retry | M1 | Labeled eval baseline; budget race/unknown-spend handled; quality gates enforced |
| M3 Product experience | Elegant responsive code viewer, search, review, publication controls, export, freshness and audit | M2 | Accessible full E2E; public opt-in/revocation/export tests pass |
| M4 Operable private beta | Metrics, purge/restore, threat tests, support diagnostics, manual entitlement grants | M3 | Synthetic SLO/load checks, restore drill, zero critical isolation/XSS issues |
| M5 Paid SaaS launch | Checkout/portal, webhook/reconciliation, quotas/plan lifecycle, privacy/terms/support | M4 | Payment failure matrix, accounting review, incident/recovery readiness |
| M6 P1 expansion | Webhook rebuild, language/level variants, sandboxed SCIP, batch/BYOK, ZIP upload | M5 + demand evidence | Each feature has isolated risk/quality/performance exit gate |

Recommended first coding instruction: “Implement M0–M1 only, using replay fixtures. Start from the tenant schema and auth policy, then complete a private end-to-end snapshot/build/viewer. Do not add model billing until the outbox and tenancy tests pass.” This avoids integrating every service before the core isolation and job semantics are observable.
