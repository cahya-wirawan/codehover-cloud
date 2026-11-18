# Security, privacy and compliance engineering

## Data inventory and trust boundaries

Sensitive data: OAuth identity/profile/email, workspace membership, GitHub installation association, private source files, README/comments, generated explanations that may reveal source, object exports, provider keys if BYOK, billing metadata and audit records. Repository metadata/path names can also be confidential. Public/private classification applies to source, guide, search results, card payload, KB JSON, manifests, ZIP files and metadata alike.

Trust boundaries: browser/API; workspace/resource; OAuth/provider callback; acquisition/parser; worker/model; object delivery; webhook/billing entitlement; public/private publication. Authorization is enforced before fetching object bytes, computing search results or returning aggregate counts. Opaque IDs reduce predictability but do not replace checks. Public slug resolution must not expose unpublished build IDs or internal listing endpoints.

## Threats and required controls

| Threat | Required control | Verification |
|---|---|---|
| IDOR/cross-tenant join or cache | Membership checks, tenant-composite FKs, RLS, workspace-prefixed caches/objects | Swap every resource ID across tenants in API/search/export/job fixtures |
| OAuth CSRF or account takeover | Server state, nonce/OIDC checks, PKCE when supported, secure sessions, explicit linking | Replay state, issuer/audience mismatch, unsolicited linking |
| GitHub installation spoofing | Verify installation access and initiating GitHub principal; state bound to workspace | Supply someone else's installation ID and reject |
| Repository SSRF | GitHub-only provider ID acquisition, fixed endpoints, redirect host checks, private/loopback IP deny in outbound layer | Redirect/DNS and alternate-scheme tests |
| Archive traversal/decompression bomb | Stream byte/entry limits; normalize once; reject absolute/`..`, links, devices, duplicates, NUL, excessive nesting; extract regular files only | Hostile tar/ZIP corpus and hard resource limits |
| Malicious code/indexer config | Never run project commands/hooks/install; read-only source; no network/credentials; unprivileged bounded parser process | Test postinstall/hook/config execution attempts |
| Prompt injection | Delimit untrusted source; no tools; fixed schema; immutable source references; no secret-bearing context | Injection fixtures and provider-body log checks |
| Generated/stored XSS | Escape source; sanitize Markdown; disallow raw HTML/scripts/iframes; safe URL schemes; strict Mermaid; CSP; no eval | Adversarial source/card/guide SVG/Markdown fixtures |
| Static artifact origin takeover | Controlled viewer for hosted data; exported HTML isolated; never tenant HTML at auth origin | CSP/origin/cookie and content-type tests |
| Secret disclosure to model | Hard excluded secret paths, entropy/pattern preflight warnings, admin-reviewed provider consent | Synthetic key fixtures, excluded file absent from all prompts |
| Queue replay / duplicate spend | SQL leases/fencing, task idempotency, reservation ledger, attempt reconciliation | Crash at publish/ack/provider/settle boundaries |
| Malicious publication | Admin-only public confirmation; immutable audit; no auto-public mode by default | Public/private transition matrix |
| Billing abuse/forged webhook | Raw-body signature verification, dedupe, authoritative subscription reconciliation | Out-of-order, duplicate, forged event suite |
| Resource exhaustion | Per-input limits, rate/concurrency controls, stage timeouts, spend caps | Limit tests under multi-tenant load |

Hard exclusions include `.git/**`, `.env` and `.env.*` except explicit safe example files after review, private key/certificate key patterns, credential stores, `.npmrc`/`.pypirc`/cloud credential files, dependencies, binaries and submodules. Heuristics do not guarantee secret detection; UI must explain that users must review selected source. Detected high-confidence credentials block generation for that file until excluded; do not automatically send it to a model to ask whether it is a secret. No code/source in third-party analytics, error session replay, browser remote logging or support ticket prefill.

## Sessions, encryption and delivery

TLS at ingress and production database/broker/provider links. Encrypt storage/volumes/backups with managed keys; GitHub App private key and OAuth secrets in secret manager with rotation. Never place secrets in VITE variables, build args, committed env files, URLs or queue payloads. Same-origin CSP baseline `default-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'`, narrow connect/style/font policies after build validation. `script-src` stays `'self'` without `unsafe-inline`/`unsafe-eval`. CodeMirror and Mermaid inject style elements, so a static SPA needs `style-src 'self' 'unsafe-inline'` unless the gateway adds a per-response nonce pipeline (CodeMirror accepts a nonce through `EditorView.cspNonce`); `deployment/nginx.conf` ships that baseline. Self-host fonts/diagram scripts; no third-party script requests from private viewer. Set nosniff, strict referrer policy, HSTS in production and no-store on private/source responses. Exports use attachment content disposition with sanitized filenames and reauthorized streaming endpoint.

Repository author grants workspace processing authority, not ownership transfer. Display source processor, model region/retention, scope and publication implications before first generation. Do not train on repository data. Allow only providers whose contractual settings satisfy the selected tenant policy; provider data handling is an operational procurement gate, not a guessed “zero retention” claim.

## Lifecycle and controls

Active source/artifact purge ≤24h after deletion; scratch ≤24h, exports 24h, failed build data 7d, generation cache 30d, build events 30d, audit 180d, backup aging ≤35d as proposed defaults. Billing retention is separately configured following jurisdiction/accounting review. As implemented in M4, a deletion is due five minutes after the request: a worker whose lease was fenced stops at its next heartbeat (≤20 s) and commits nothing, and the purge sweeps the object prefix before and after its database step, so late writes are removed too. Deletion disables delivery immediately and fences active jobs; workers check tombstone before storing or publishing. Restores replay tombstones before traffic. Downloads already obtained by users/public visitors cannot be recalled; disclose that when publishing/exporting. Do not imply revocation can remove copies from the internet.

User data export contains profile, memberships, projects and usage plus authorized source/artifacts separately; it must exclude other workspace members' private account information. Operators see job IDs and counts by default; source access requires time-bounded explicit support grant, reason, audit and revocation. Incident runbook covers containment, key rotation, access log review, scope assessment, notification decision and recovery tests.

## Launch review checklist

This is a product/engineering checklist, not a legal conclusion. Confirm controller/processor roles, lawful basis, privacy notice, DPA, subprocessors, international transfers, user access/erasure/export process, cookie necessity/analytics consent, security incident process, repository licensing/attribution, terms on publication rights, AI-generated documentation disclosure, accessibility review and payment/tax responsibilities. The service targets professional developers, not children; no child-directed flows or medical claims. Obtain jurisdiction-specific review before a public paid launch. Policy texts must match actual retention and provider configuration.
