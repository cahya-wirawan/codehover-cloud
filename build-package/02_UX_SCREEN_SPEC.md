# UX screen specification

All screens are Proposed. Navigation: workspace switcher; Projects; Builds; Usage; Members; Settings. Project navigation: Overview, Code, Guide, Builds, Settings. Global command menu supports project/file/symbol navigation. Workspace role and current project visibility are always discoverable.

## Screen inventory

| ID / route | Purpose and components | Actions / role | Validation and special states |
|---|---|---|---|
| S01 `/` | Professional marketing page; annotated-code demo, workflow, privacy, plans, CTA | Any user: sign in, open authorized demo | Demo uses redistributable fixture only; no private project previews |
| S02 `/login` | Google/GitHub buttons, terms/privacy links, return destination | Anonymous: provider redirect | Provider denied/expired state; retry preserves safe relative return path |
| S03 `/onboarding` | Workspace name and slug; connect GitHub card; confidentiality notice | New user: create personal workspace | Name 2–80 chars; slug lowercase 3–48; conflict inline; connection may be skipped |
| S04 `/w/:wid/projects` | Search/filter; project rows with repo, visibility, last commit, age, build health | Viewer read; editor new project | Empty: connect first repository; loading row skeleton; revoked connection badge |
| S05 `/w/:wid/connections` | Separate account sign-in and repository access; installations and repo counts | Admin connect/disconnect | GitHub authorization boundary and limited access shown; spoofed installation callback rejected |
| S06 `/w/:wid/projects/new` | Repository picker; branch/tag/commit picker; project name | Editor import | Select only authorized repo IDs; unavailable/ref moved prompts refresh; scanning phase shows safe counts |
| S07 `/p/:pid/configure` | File tree with include/exclude preview; model preset; scope, cap; explanation coverage estimate | Editor save/estimate | Blocked files cannot be re-enabled; glob syntax errors; clear reasons for skipped files |
| S08 `/p/:pid/estimate/:eid` | SHA, scope checksum, token/cost range, stages, data destination, cap and credits | Editor start build | Expired/mismatched estimate disables Start; provider consent required; insufficient funds links Usage |
| S09 `/p/:pid/builds/:bid` | Stage timeline, counts, warnings, current spend/cap, cancel, retry, diagnostics | Viewer read; editor cancel/retry | Queued vs stalled vs rate-limited distinct; partial results labeled; reconnect from event cursor |
| S10 `/p/:pid` | Snapshot overview, concepts, reading order, coverage and freshness, variant picker | Viewer browse; admin publish | No ready build: status CTA; private/public label; missing descriptions show source-only status |
| S11 `/p/:pid/b/:bid/code/*path` | Three panes: tree 248px; code flex; outline/details 280px. Breadcrumbs, SHA, search, notes switch | Viewer hover/pin/follow reference/open original | Ambiguous reference chooser; excluded file unavailable; parse failure still shows escaped plain source |
| S12 `/p/:pid/b/:bid/guide/:slug` | Guide navigation, prose/diagrams, citation chips and support legend | Viewer open exact source; editor report | Citation popover states support/reason/span; invalid citation non-navigable and flagged |
| S13 `/p/:pid/b/:bid/search` | Query and kind filter; ranked result snippets with name/file and confidence | Viewer navigate | Empty results suggest identifier terms; query ≤200 chars; no search-history retention by default |
| S14 `/p/:pid/builds/:bid/review` | Coverage, source-location validation, claim checks, skipped/failed tasks, waiver list | Editor inspect; admin waive/publish | Verification unfinished blocks publication; waivers require reason and remain visible |
| S15 `/p/:pid/publish` | Version selection, private/public radio, disclosure summary, confirm and rollback | Admin/owner | Default private; public requires typed project-name confirmation; previous build remains live until atomic switch |
| S16 `/w/:wid/usage` | Current period usage, credits, reservations, ledger and plan; billing portal | Member summary; owner billing | Pending webhook shows “processing”; never infer entitlement from success URL |
| S17 `/w/:wid/members` | Members, roles, invitations, workspace-wide access explanation | Admin manage; owner ownership | Expired invitation/revoke/resend; cannot remove/demote final owner; self-role change reauthorizes |
| S18 `/account/security` | Linked providers, sessions, logout-all, export/delete account | Self | Reauthentication for provider link/delete; cannot unlink last sign-in provider |
| S19 `/p/:pid/settings` | Name, source/ref, exclusions, build defaults, access suspension, retention, export/delete | Editor config; admin delete | ETag conflict with refreshed diff; deletion confirmation; connection revocation blocks new builds |
| S20 `/sites/:slug/*` | Published site with same code/guide reading experience | Public visitor only if explicitly public; otherwise member | Every asset/source/KB request authorized; withdrawn public site returns 404; no content in preview metadata |
| S21 `/ops/jobs` | Operational counts, lease/dead-letter tools, redacted IDs | Scoped operator | No source browsing by default; approved time-bounded support access audited |

## Component contracts

Hover card shows kind/signature, summary, details, params, returns, notes, Uses/Used by and confidence, with exact-source and guide links. Delay pointer open 200 ms; focused symbol opens by Enter, pins with Enter/click, closes by Esc; moving into the card must not close it. Cap width 480px and height 60vh; interactive popover is a dialog rather than a tooltip. Ambiguous target list displays all candidates with file path and resolution basis. Source ranges are 1-based inclusive lines; browser columns use Unicode scalar offsets in the API, converted to editor UTF-16 offsets at a single tested boundary.

Build timeline displays pending/running/completed/skipped/failed stages with counts, not fake animated progress. Spend distinguishes estimate, reserved upper bound and settled usage. Error diagnostics use request/build IDs and safe reason, with a Retry action only when retryable.

## Shared states for every authenticated screen

Loading: structural skeleton and aria-busy; never show a previous workspace's data during switching. Empty: one task-specific explanation and primary action. Error: inline recoverable state, safe code, Retry, trace ID; 401 returns to sign-in, 403 explains permission, missing or unauthorized resource IDs return generic 404 where enumeration matters. Offline: persistent banner, retained visible in-memory snapshot, disable writes, do not queue publication/build actions. Session expiration clears query cache before navigation. Reconnection refetches membership and build state before controls re-enable. Each screen has an explicit denied role state, keyboard order, visible focus, landmarks and descriptive headings.

Analytics: `login_completed`, `workspace_created`, `repository_connected`, `estimate_viewed`, `build_requested`, `build_viewed`, `source_opened`, `guide_opened`, `publication_changed`, `export_requested`. Record identifiers as pseudonymous internal IDs, never file paths, code, query text, email or generated prose.
