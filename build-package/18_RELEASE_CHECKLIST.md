# Release checklist

## Product and quality

- [ ] Critical FR/acceptance scenarios trace to passing tests; limitations documented.
- [ ] Source snapshot SHA/provenance, scope, exclusions and partial coverage visible.
- [ ] No invalid/unchecked citation represented as supported; quality policy enforced.
- [ ] All generated/private artifacts covered by authorization; public confirmation and unpublish verified.
- [ ] Desktop/mobile, light/dark, keyboard/touch and screen-reader review complete.
- [ ] Offline export includes local assets and license notices; compatibility claims tested.

## Security and data

- [ ] OAuth apps/callbacks verified in staging; explicit identity linking and installation authority tested.
- [ ] Runtime SQL role/RLS and cross-tenant negative suite pass.
- [ ] Secrets not present in git, image layers, VITE output, logs, broker payloads or URLs.
- [ ] Archive/parser sandbox, SSRF, XSS/Mermaid/CSP and quota tests pass.
- [ ] Source processor configuration/consent, privacy notice, DPA and retention match deployment.
- [ ] Source deletion/tombstones and backup aging verified; support access controlled.
- [ ] Dependency/image scan and SBOM reviewed; exact locks and image digests captured.

## Operations and paid gate

- [ ] Expand-compatible migrations executed once with least-privilege runtime role.
- [ ] Worker restart/outbox duplication/fencing and model timeout reconciliation tested.
- [ ] Metrics/alerts, queue limits, provider budgets and kill switch exercised.
- [ ] PG18 backup/PITR plus object recovery drill meet target; rollback documented.
- [ ] GitHub revocation/rename/transfer lifecycle and publication suspension tested.
- [ ] Checkout, portal, signature verification, webhook ordering, renewals, refunds and daily reconciliation pass before charging customers.
- [ ] Terms, billing/tax/cancellation/support and incident procedures reviewed.
- [ ] Production TLS/secret manager/storage IAM/egress controls configured; management ports private.
- [ ] Release tagged `vX.Y.Z`; the bump is at least the change class CI computed from the OpenAPI diff, schema compatibility and replay fixtures (document 23).
- [ ] Release metadata records app version and conformed spec version (`SPEC_VERSION`), OpenAPI, engine, prompt-set and parser versions, contract `$id`s, model presets, price catalog, Alembic head and known limits.
- [ ] No artifact-schema reader is removed while a retained or published build still references that MAJOR.

Block release for any known critical cross-tenant access, secret leak, source-origin script execution, unbounded spend or unsafe public publication. A planning-package validation result does not satisfy these implementation gates.
