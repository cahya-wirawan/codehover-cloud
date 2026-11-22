# Product requirements — codehover-cloud

## Vision and problem

Help developers understand an unfamiliar repository through a navigable explanation website grounded in a precise source snapshot. Today a user must install tooling, choose an LLM, configure keys, manage generation and host the result. Teams also need to control who can see private source, how much generation costs, and which commit documentation describes.

The service owns the complete import → scope → estimate → build → review → publish → update lifecycle. Reading a completed site never requires a model call. The product explains code; it does not execute the imported application or claim to prove correctness.

## Personas and jobs to be done

| Persona | Job | Main success signal |
|---|---|---|
| New team member | Learn architecture and find the implementation behind a concept | Finds and explains a task-relevant definition with source evidence |
| Maintainer/lead | Publish trustworthy onboarding documentation without manual annotation | Reviews a build and safely shares it with the team |
| Consultant/reviewer | Understand a client codebase while preserving confidentiality | Private snapshot and reproducible export with a bounded budget |
| Open-source educator | Produce a readable walkthrough for learners | Public opt-in site, beginner explanations and languages in P1 |
| Workspace owner | Control members, usage and data lifecycle | Predictable usage, revocable access and auditable changes |

Differentiation: source-first hover reading; explicit citation support and reference confidence; immutable commit provenance; private-by-default hosting; portable outputs; preflight estimates and enforced budget reservations.

## Scope and release boundaries

MVP includes OAuth, personal/team workspaces, read-only GitHub App installation, repository selection, exact commit snapshots, scan exclusions, preflight estimates, one approved hosted LLM and replay testing, queued builds with cancellation/retry, code/guide viewer, search, verification review, publication access controls, static/KB exports, usage ledger, and owner/member roles. A closed beta can use manual entitlement grants; a paid public SaaS release additionally requires Stripe checkout, portal, validated webhooks and reconciliation.

P1 adds GitHub push rebuilds, incremental dependency invalidation optimization, German/Indonesian output, beginner/expert variants, sandboxed Python/TypeScript/Rust SCIP, batch API processing, BYOK, richer retention controls, ZIP repository import and public GitHub repositories without the App (FR-028). P2 adds custom domains, enterprise identity, additional VCS providers, advanced graph views and customer-managed inference. Hosted MCP is implemented ahead of P2 (06 `/mcp/{slug}`): a published site's documentation as read-only MCP tools, public sites anonymously and private ones with a member's personal access token. Languages not passing parser fixtures are shown as plain text rather than advertised as semantically supported. Parsed languages are Python, JavaScript, TypeScript (with TSX) and Rust.

Non-goals: source-code editing, PR creation, autonomous shell agents, compiling/running tenant projects, installing their dependencies, general code chat, security-audit certification, live IDE replacement, training models on user repositories, or changing repository visibility. Webhooks never auto-publish publicly by default.

## Functional requirements

| ID | Requirement and acceptance boundary | Priority |
|---|---|---|
| FR-001 | Sign in with Google or GitHub; logout; revoke sessions; explicit linked-account flow; no email-only merge | P0 |
| FR-002 | Create workspace, invite member, assign owner/admin/editor/viewer; last owner cannot leave | P0 |
| FR-003 | Connect a GitHub App installation separately from login; list only authorized repositories | P0 |
| FR-004 | Create project bound to repository numeric ID; select branch/tag/commit; resolve and display immutable commit SHA | P0 |
| FR-005 | Scan without running repository code; preview file/language counts, exclusions, unsupported/binary/oversize reasons | P0 |
| FR-006 | Configure include/exclude globs, max files, language/level, guide size, model preset and spend ceiling | P0 |
| FR-007 | Estimate tokens/cost and scope; disclose assumptions and price revision; expire estimate after 30 minutes | P0 |
| FR-008 | Start only an approved exact snapshot/config estimate; atomically reserve usage capacity | P0 |
| FR-009 | Track queued/running stages and costs; cancel safely; resume completed stages; retry creates a traceable attempt | P0 |
| FR-010 | Generate per-file summary, definition cards, parameters/returns/notes, line annotations and relationships | P0 |
| FR-011 | Produce overview/key concepts/reading order and 3–8 topic guide pages with source citations | P0 |
| FR-012 | Validate source locations and snippets deterministically; independently classify claim support; preserve unknown/failed checks | P0 |
| FR-013 | Browse immutable build with tree/outline, keyboard-pinnable hover cards, inline notes, source links and line anchors | P0 |
| FR-014 | Search names, files, explanations and guides within the authorized build; exact identifier boosts and relation expansion | P0 |
| FR-015 | Show verification summary, ambiguous references, skipped files, coverage, model/prompt/engine versions and commit | P0 |
| FR-016 | Review and publish ready build atomically; private default; public publication requires explicit disclosure confirmation | P0 |
| FR-017 | Change access, unpublish, revoke access immediately, switch published version, retain publication audit events | P0 |
| FR-018 | Export selected build as self-contained static ZIP or versioned KB JSON; warn that downloads contain source | P0 |
| FR-019 | Report freshness versus latest observed branch SHA; user-triggered rebuild; unknown when GitHub is unavailable | P0 |
| FR-020 | Record provider usage, budget reservations, charges and adjustments; show workspace ledger | P0 |
| FR-021 | Project/account export and deletion with active-job cancellation, object/cache purge and backup aging | P0 |
| FR-022 | Paid launch: plan checkout, renewals, cancellation, portal, signed webhook ingestion and entitlement reconciliation | P0 paid gate |
| FR-023 | Audit changes to members, connections, builds, publications, billing and deletion; redact secrets | P0 |
| FR-024 | Rate-limit API/exports/builds, enforce tenant isolation and constrain malicious repository content | P0 |
| FR-025 | Webhook-driven builds with branch filtering, deduplication and debounce; owner-defined approval policy (M6: per-project push policy off/snapshot/build with a push cap; never publishes) | P1 |
| FR-026 | Language/level variants keep page and source anchor; immutable citations survive translation (M6: en/de/id, from 0.6.9 the 30 most spoken languages by total speakers (Ethnologue 2026 as listed by Wikipedia): en, zh, hi, es, ar, fr, bn, pt, id, ur, ru, de, ja, pcm, arz, mr, vi, te, sw, ha, tr, pnb, tl, ta, yue, wuu, fa, ko, am, th (ISO 639-1, else ISO 639-3; `prompts.LANGUAGES`), × standard/beginner/expert; a variant reuses its base build's snapshot, scope and guide plan; one estimate may name up to three languages, which start the base and a variant per further language together; a site and an offline ZIP switch between them; from 0.6.8 further languages may instead be translated from the first build's text, `translate`) | P1 |
| FR-027 | GitHub token revocation blocks ingestion and publication; installation removal suspends affected private hosted sites until owner review | P0 |
| FR-028 | A project can read a public GitHub repository without the GitHub App: an editor enters `owner/name` or its github.com address, the repository must be public whenever a ref is resolved or source is downloaded, builds start by hand (no push rebuilds), and source downloads are anonymous | P1 |
| FR-029 | A public site of a repository read through the public source (FR-028) needs a license that permits redistribution: GitHub's license answer at the build's exact commit must be on the permitted list (open-source licenses, `app/modules/projects/licenses.py`), and the license file must be part of the build so the site carries its notice. The site names the license and links to the file. No license, an unidentified one or any other license allows only a private (members-only) site. App-granted and uploaded repositories are not checked | P1 |
| FR-030 | GitHub-like owners (0038): every person has a handle, and organizations are a level above workspaces with their own handle in the same global namespace (1–39 lowercase letters, digits and single hyphens; route words reserved). A person confirms or changes the handle suggested at first sign-in (from the GitHub login or email). Every workspace belongs to a person or an organization; an organization's owners and admins are owners and admins of all its workspaces, its plain members see only the workspaces they are added to. Organizations invite people by handle; the person accepts in the app. A released handle (rename, deleted account or organization) stays reserved for 90 days. Sites live under their owner at `/sites/<owner>/<project>` (0039), project slugs being unique per owner; pre-0039 one-segment addresses redirect for a year, and a renamed owner's old handle keeps answering while it stays reserved for them. From 0040 the plan belongs to the owner: an organization's workspaces (or a person's own) share one plan, trial and allowance, with limits counted across them, managed by the organization's owners (or the person) | P1 |

## Critical product policies

Workspace membership grants access to every project in that workspace in MVP. Display this when adding a private repository or inviting a member. Separate workspaces are required for different client audiences; per-project ACLs are P1. They are implemented after M6: admins can restrict a project to chosen members (09). GitHub repository permissions and workspace permissions are separate grants. Repositories must be connected by a user whose repository authority and installation association are verified server-side. Never trust an installation ID supplied by a browser.

Private source remains private unless an owner/admin explicitly chooses Public, confirms disclosure of source and generated text, and the audit record is stored. A member inviting others cannot silently make a site public. Public sites may disclose the entire selected source; exclusions must be reviewed before generation as well as publication. Private sharing links are not in MVP; membership is the sole private-sharing mechanism.

Partial generation produces a reviewable draft with coverage details. Failed mandatory verification or unavailable support checks prevents publish; a completed checker can return unsupported claims, which must be removed/regenerated or explicitly waived by an admin with an audit reason. Waivers remain visible in the site. Unsupported or unchecked content never receives a supported badge.

## Initial limits and UX behavior

MVP safety maxima: GitHub snapshot archive 100 MiB downloaded, 250 MiB expanded, 10,000 archive entries, path depth 30; 2,000 scanned source files, 200 selected explanation files, 512 KiB per source file, 3,000 lines/file, 1 million selected source characters. Limits are checked while streaming and expanding, before provider calls. Tenant plan limits may be lower. At most one active build/project and two/workspace by default. Guide pages 3–8, hard maximum 8. One English standard variant initially. Exclusion globs cannot override blocked secret paths or symlink rules.

Build cap defaults to the lower of workspace remaining credits and configured owner limit. Estimates show a range, never a guaranteed invoice. Every outbound request reserves its maximum configured token cost; generation stops before a new request would exceed the remaining cap. In-flight requests may complete after cancellation; show actual consumed usage.

## Non-functional requirements

| ID | Target | Measurement |
|---|---|---|
| NFR-001 | Zero cross-workspace data exposure | Negative API, object, search, job and cache tests in CI |
| NFR-002 | 99.5% monthly API/hosted-viewer availability initial target | Synthetic probes excluding planned maintenance |
| NFR-003 | API reads p95 <500 ms at 50 concurrent beta users | Excludes GitHub/model calls, downloads and SSE |
| NFR-004 | Viewer first useful content <2 s on agreed test connection | Warm 100-file build, cold and warm runs recorded |
| NFR-005 | Cached card opens <100 ms; lexical search p95 <700 ms | 20k-symbol fixture, bounded payload and virtual rendering |
| NFR-006 | Worker crash never promotes partial output or duplicates settled usage | Fault injection and lease tests |
| NFR-007 | Progress visible within 3 s of committed state | SSE plus polling fallback |
| NFR-008 | WCAG 2.2 AA target for core flows | Automated checks plus keyboard/screen-reader review |
| NFR-009 | PostgreSQL recovery RPO ≤15 min, RTO ≤4 h | Managed PITR and recorded restore drill before paid launch |
| NFR-010 | Build reproducibility metadata always retained | Snapshot/config/prompt/model/parser/engine hashes per build |

Build-duration target: a 20-file/20k-line fixture should usually complete within 10 minutes on an interactive provider tier, excluding queue time; this is a capacity experiment, not an SLA. Progress states use counts rather than unreliable time promises.

## KPIs and validation

Activation: first successful private build viewed within 24 h of account creation. Measure eligible-user denominator, funnel drop-off, and median time to useful view. Initial experiment goals: ≥60% activation among invited beta users; ≥90% technically successful builds excluding deliberate user cancellation and input rejection; ≥95% source-location validity; zero claims with invalid citations published as supported; ≥80% sampled explanations judged useful and factually supported by maintainers; ≥30% of activated workspaces open docs again within seven days. Report sample sizes and confidence intervals instead of presenting small samples as established performance.

Cost KPIs: provider cost/successful build, cost per explained KLOC, cache-hit rate, failed-call spend, reserved versus settled usage, and gross margin including storage/egress. Trust KPIs: unsupported/partial/unchecked fractions, correction rate, ambiguous-link rate and freshness coverage. Public conversion and revenue targets are set after beta evidence.

## Accessibility, localization and support

Every hover has focus/click/touch equivalents, pin/close controls, labeled content and predictable focus return. Color is never the only state cue. Code supports horizontal scroll and preserved indentation; prose reflows at 200% zoom. English UI initially, translation-ready message keys and locale-aware dates/currency. German and Indonesian are planned output languages. Support includes build diagnostic IDs, sanitized errors, provider outage notice, and a report-inaccurate-explanation action linked to the immutable build/definition.
