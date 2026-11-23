# Changelog

All notable changes to this build package. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/); versions follow the "Build package" rules in `23_VERSIONING_AND_RELEASES.md`. New entries go under Unreleased, grouped as Added, Changed, Deprecated, Removed, Fixed or Security; `python3 tools/assemble.py bump <major|minor|patch>` turns them into a release.

## [Unreleased]

## [0.9.0] - 2026-11-23

### Changed

- 02, 05, 06: a workspace needs only a name. Its slug is internal (links use the workspace ID, sites `/sites/<owner>/<project>`), so the "New workspace" form drops the address field; `POST /workspaces` makes the slug from the name when none is given, with a random suffix when it is taken. Deleting a workspace is confirmed by typing its name instead of its address.

## [0.8.3] - 2026-11-23

### Added

- 02: a context switcher at the top of the sidebar, like GitHub's: you or one of your organizations, with open invitations counted. The workspace menu, the workspaces page and new workspaces follow the chosen owner; creating an organization has its own page.

## [0.8.2] - 2026-11-22

### Security

- 05, 06: while a renamed project's old address still leads to it, only a project of the same workspace may take that address; another workspace of the same owner gets `SLUG_TAKEN`, so an organization's workspace admins cannot take each other's links.
- 05: taking an address and renaming to it are serialized per owner and address (an advisory lock), so a concurrent rename and new project cannot both pass the check.

## [0.8.1] - 2026-11-22

### Added

- 02, 05, 06: admins change a project's address (`PUT /projects/{pid}/address`, migration 0041), the <project> of `/sites/<owner>/<project>`. The old address keeps leading to the site for 90 days, inside the owner's namespace only.

## [0.8.0] - 2026-11-22

### Added

- 01, 02, 05, 06, 09: people and organizations (FR-030, migration 0038). Every person has a handle, suggested at first sign-in and confirmed once, in one global namespace with organizations; renamed or deleted handles stay reserved for 90 days. Organizations are a level above workspaces: every workspace belongs to a person or an organization, an organization's owners and admins are owners and admins of all its workspaces, and plain members see the workspaces they are added to. Organizations invite people by handle. 
- 01, 05, 06, 11: one plan per owner (migration 0040). An organization's workspaces, or a person's own, share one Stripe customer, subscription, trial and monthly allowance; project, seat and active-build limits count across them. The organization's owners (or the person) manage the plan, and everyone in a workspace reads it. A billing suspension takes all the owner's sites offline.
- 01, 02, 05, 06, 09: sites live under their owner, `/sites/<owner>/<project>`, with the MCP endpoint at `/api/v1/mcp/<owner>/<project>` (migration 0039). A project's slug is unique among its owner's projects; publishing no longer asks for an address. Old one-segment addresses become aliases that redirect for a year (`GET /site-aliases/{alias}`), and their MCP endpoints keep answering; a renamed owner keeps answering at the old handle while it stays reserved for them. The free-form `public_slug`, its 0036 reservations and `DELETE /ops/slug-reservations/{slug}` are removed: an address can no longer name another owner's site.

## [0.7.0] - 2026-11-22

### Added

- 21, 22: TypeScript imports through path aliases (`@/…`) resolve in SCIP. The sandboxed index worker reads `baseUrl` and `paths` from the repository's `tsconfig*.json` / `jsconfig*.json` (following `extends` inside the snapshot), and gives each aliasing config a project of its own, so each app in a monorepo resolves its own `@/*`. No other option of the repository's config is loaded. Engine 0.4.0.
- 05, 06, 09, 22: MCP sign-in (migration 0037). An MCP client can now sign its person in to a private hosted site through the browser instead of a pasted token: codehover acts as an OAuth 2.1 authorization server with RFC 9728 and RFC 8414 discovery, dynamic client registration (public clients), PKCE S256, a consent page, one-time codes of 60 s, access tokens of one hour bound to the site's project, and refresh tokens of 30 days that rotate, with replay detection for codes and refresh tokens. Connected apps are listed and disconnected on the workspace page. Without a token, a private or unknown site now answers 401 with `resource_metadata` instead of 404, so clients can start sign-in; both still look alike. The gateway forwards `/.well-known/oauth-` to the API.
- 06: hosted MCP also serves resources (the overview, each guide page, and source files through a template, under `codehover://<slug>/...`) and three prompts (`tour`, `explain`, `where`).

### Fixed

- 07, 19: a language variant's guide sections and purposes were the base's, in the base's language. Each variant in another language now translates the base's guide plan once (P07 kind `plan`, same template): only titles, sections and purposes change, and a translation that changes a slug or a source range keeps the base's text. Translated estimates count that call.

### Security

- 05, 06: a released site address stays reserved for its workspace for 90 days (migration 0036 `slug_reservations`). Before, unpublishing, choosing a new address or deleting the project let another workspace publish at the old address at once and receive its readers, links and MCP clients. Each project keeps the 3 addresses it released last, so renaming a site through many addresses cannot hold them all, and operators can end one early (`DELETE /ops/slug-reservations/{slug}`).

## [0.6.9] - 2026-11-22

### Added

- 01, 02, 06, 07: languages are the 30 most spoken by total speakers (Ethnologue 2026, as listed by Wikipedia), up from English, German and Indonesian; site routes and MCP tools accept the same codes. English, German and Indonesian keep their prompt names, so their cached outputs stay valid. The generation panel makes English the first language, adds further ones from a dropdown and translates them by default.
- 02: a repository Markdown file opens formatted in the code view, with a Formatted / Source switch; right-to-left prose takes its paragraph's direction.

### Security

- 02, 19: Mermaid diagrams are checked in the browser too before Mermaid sees them (no click, style, classDef, linkStyle, init directives, HTML or frontmatter configuration, at most 20,000 characters); a refused diagram shows as its source. Repository Markdown files, now rendered formatted, never pass the backend's guide allowlist, which also refuses frontmatter configuration now.

## [0.6.8] - 2026-11-22

### Added

- 06, 07, 13, 19 (P07), prompt set 0.6.0: translated language variants. With `translate: true`, a several-language build (or a `variant_of` in another language at the same reader level) translates the first build's validated explanations, overview and guide pages instead of writing them from the source. Each stage waits for the base's; translations must keep the source's structure, identifiers, ranges, citations and links exactly; units the base could not write are written from the source; translated pages are still claim-checked. Estimates price each translated language separately (`per_translation_micro_usd`), and a family's cap is shared in proportion to the estimates.

## [0.6.7] - 2026-11-21

### Fixed

- 06: cancelling the base of a several-language build now cancels its active language variants too. Before, a variant kept running (and calling models) until it reached its plan stage and failed with `base_unavailable`. Cancelling one variant still stops only that variant.

## [0.6.6] - 2026-11-21

### Added

- 01, 02, 05, 06, 09, 13, 15: hosted MCP. `POST /mcp/{slug}` serves a published site's documentation as eight read-only MCP tools (overview, search, get_definition, find_references, get_file, read_lines, list_guide, read_guide_page), the same as codehover's local server, over Streamable HTTP without sessions. Public sites answer anyone; private sites answer a personal access token of a member who may see the project (`/workspaces/{wid}/access-tokens`, migration 0035; digest stored only, shown once, scope `mcp:read`, at most 365 days, revoked when the member leaves). The publication panel shows the site's MCP address and the command to add it; the workspace page manages tokens.

## [0.6.5] - 2026-11-21

### Added

- 05, 11, 12, 22, deployment: owner notices by email. Owners and admins are emailed when a subscription becomes past due, when a dispute opens or is lost, and when a site stops serving for any reason. Notices are recorded once per state change in the changing transaction (`owner_notices`, migration 0034; site suspensions through a statement-level trigger on `publications`) and sent by the worker over SMTP (`SMTP_HOST`, `SMTP_PORT`, `SMTP_TLS`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`; all optional), at least once with backoff. Compose adds a local Mailpit (`mail`) that the worker uses by default, so nothing leaves the machine. A metric counts deliveries by outcome.

## [0.6.4] - 2026-11-21

### Added

- 01, 02, 05, 06, 10, 13: several languages in one request (FR-026), like the codehover CLI. An estimate may name up to three languages; starting it creates the base build and a variant build per further language together, each with an equal share of the cap, counted as one active build and allowed to run beside the base (migration 0032; the server marks them `family`). A family variant's plan waits for the base's plan. A publish approves and records the ready language variants of the build's family (migration 0033, each through the review gate and the publish's own acknowledgment); its site serves them by `language` with a switcher (`/published/{slug}/variants`). A static ZIP carries the build's other languages with a switcher.

## [0.6.3] - 2026-11-21

### Added

- 07: explain batches run for OpenAI (a JSONL file on `/v1/responses`, 24-hour window) and Gemini (inline `batchGenerateContent`) as well as Anthropic, at each model's catalog batch price; the catalog now accepts batch prices for those providers and still refuses OpenRouter. A submission that cannot have reached the provider (`batch_not_sent`) releases its holds instead of keeping them as unknown.

## [0.6.2] - 2026-11-21

### Added

- 07, 13, 21: C and C++ repositories are parsed (parser 0.5.0, tree-sitter-c 0.24.2, tree-sitter-cpp 0.23.4): functions, macros, types, classes, namespaces and methods, with doc comments and parameters, and `.h` read as C++ beside C++ sources. Uses resolve by name; there is no C/C++ indexer. The viewer highlights both languages.

## [0.6.1] - 2026-11-21

### Changed

- 02, 10, 13, 22, `contracts/kb-export.schema.json` 1.1.0: the offline ZIP's reader is the product's own reading view (guide with Markdown, tables and diagrams; code with notes and cards at every use; search), built from the frontend's `export.html` entry and shipped in every static ZIP; the backend image builds it. The KB adds each file's references and each guide page's section and purpose (optional fields, so the change is additive). A missing viewer fails the export with `export_viewer_missing`. The hand-written `viewer.js` is removed.

## [0.6.0] - 2026-11-21

### Added

- 02, 05, 06, 19, `contracts/guide-plan.schema.json` 2.0.0: guide plans name a `section` for each page (required, so the contract MAJOR changes; migration 0031 stores it, '' for older pages), shown as headings in the page list. Guide pages may contain tables and link to each other with `guide:<slug>`; Mermaid diagrams are drawn in strict mode as SVG images. Symbol views list the guide pages that cite the definition (`guides`).
- 02, 17: the reading view (code, cards, guide, search and published sites) takes codehover's design: its palette and kind colours, Newsreader and Red Hat Mono, notes marked in the gutter and shown on hover or inline, cards at every use of a definition and documented parameter, a guide landing view with key ideas and reading order, linked definitions and a pager. 06: the guide index adds each reading-order file's path.
- 06: file views return `references` (each use of a definition: position, targets, resolution, ambiguity) and each definition's parameter names; guide pages return the definitions they name in backticks (`symbols`), matched by name only when exactly one definition fits.

### Changed

- 19: prompt set 0.5.0 is the default for generation builds: codehover's guidance for denser line notes, overview, planning by concept with sections, and guide pages with tables, diagrams, linked definitions and links between pages; the page prompt gets the guide plan and the definitions in its evidence.
- 00, README: the product owner decided to reuse codehover's viewer design and prompt writing guidance under its MIT licence, with the notice shipped at `/licenses/codehover.txt`; everything else stays independently implemented.

## [0.5.40] - 2026-11-21

### Fixed

- 19: guide pages that cite one line as `[[file_id:N]]` are stored as `[[file_id:N-N]]`, and any other malformed citation marker is sent back for repair before the page is stored. Both writers (Claude Sonnet 5.5 and Gemini 3.8 Flash) wrote single-line citations, which became invalid claims that blocked publication.
- 07: a checker answer cut off at its output limit is no longer repaired (the repair asked the same task under the same limit and was cut off too); its batch is checked in halves, at most twice. A Claude Haiku 5.5 check of 25 claims within 4096 output tokens left all 25 `unchecked`.
- 04, 07: a provider call waits up to 300 s for its answer (was 90 s), and 15 s to connect. A Gemini 3.8 Flash checker call was cut off at 90 s, leaving 14 claims unchecked and the build unpublishable.

## [0.5.39] - 2026-11-21

### Changed

- 02, 13: the repository overview in the guide view renders its Markdown the same way as guide pages, instead of showing it as plain text. The offline export viewer is unchanged.

## [0.5.38] - 2026-11-21

### Changed

- 02, 13, 16: guide pages in the viewer and on published sites render their Markdown (headings, lists, emphasis, code, block quotes) instead of showing it as plain text. The frontend parses with markdown-it's CommonMark preset, the parser behind the backend's P04 allowlist, with raw HTML off, into a fixed set of React elements; images show their alt text and only https and in-page links are links. The offline export viewer is unchanged.

## [0.5.37] - 2026-11-21

### Fixed

- 07: a writer call whose provider never answers no longer lets the build finish on source-only text. Before, an unknown outcome (a dropped connection or a timeout) was retried at once up to three times, each try adding a worst-case hold, and the unit then fell back to the replay answer, so a build whose every call failed ended `ready` with "No valid model explanation" everywhere. Now an unknown outcome is not retried within the call; a writer call that gets no answer (transient after its retries, or unknown) stops the stage as `provider_unavailable`, the task retries later from its checkpoints, and the build fails when its attempts run out. Checker calls without an answer still leave claims `unchecked`. Seen live: a network that reset IPv4 requests to Anthropic over about 1.5 KB.

## [0.5.36] - 2026-11-21

### Fixed

- 07: the estimate's high bound held too little for reasoning models, so a build whose cap was left at the high bound could end as `budget_exhausted`. It now counts the most guide pages the plan may write (a one-file scope was counted as one page, but the plan may write up to `guide_pages`), and for a role with an `effort` every call at the role's `max_output_tokens`, since reasoning tokens bill as output. Seen live with Qwen4 Max at medium effort, whose explanation of one chunk used 9,370 output tokens against an assumed 2,500. High bounds of reasoning presets rise several-fold; the low bound now counts the fewest pages.

## [0.5.35] - 2026-11-20

### Fixed

- 07: a build no longer ends as `budget_exhausted` while its money is there. Every call holds its worst-case cost, so with up to 4 calls in flight the holds could outgrow a cap set from the estimate, which is sized for actual costs. A call whose maximum would fit once the build's in-flight calls settle now waits for them (at most 10 minutes). The build ends only when the maximum would not fit with nothing in flight. Seen live with 4 concurrent guide calls.
- 07: after a stage stops (budget or cancellation), units already running finish, so their calls settle at actual cost instead of being cut off with an unknown outcome. A call cut off by a worker shutdown becomes `unknown` and keeps its hold. A worker shutdown now also stops the running stage, which had kept running.
- 07: a connection that never opened (refused, a failed TLS handshake or a connect timeout) is transient for Anthropic calls too, as it already was for the other providers. Before, it was `unknown` and kept a worst-case hold for a call that could not have been billed.

### Changed

- 05, 07, 11: an attempt whose outcome is still unknown 24 hours after it began is settled at its maximum and becomes `reconciled`, by the hourly retention sweep. Holds no longer stay on the allowance indefinitely.

## [0.5.34] - 2026-11-20

### Fixed

- 06: a generation build whose cap would be zero is refused instead of started. Such a build could make no provider call and stopped at once with "spending cap reached". With no allowance left and no cap given, `POST /projects/{pid}/builds` (and retry) returns 409 `ALLOWANCE_EXCEEDED`; an explicit cap of zero returns 422 `CAP_TOO_LOW`.

## [0.5.33] - 2026-11-20

### Added

- 07: OpenAI, Gemini and OpenRouter join Anthropic as catalog providers (`provider` is `openai`, `gemini` or `openrouter`). The adapters call each provider's official HTTPS API directly, without an SDK:
  - OpenAI: the Responses API with a strict JSON schema and `store: false`.
  - Gemini: the Gemini API's `generateContent` with `responseJsonSchema`, the key in a header.
  - OpenRouter: chat completions with a strict schema, no `models` fallback, `provider.max_price` set to the catalog price, `data_collection: deny` and `require_parameters`.
  - All three: cached input is priced apart, reasoning and thinking tokens count as output, and errors map onto the gateway's error kinds, including OpenRouter errors inside a 200 response.
- 07: catalog rules for the new providers. Only Anthropic models may carry a `batch_price`, since no other gateway submits batches, and Gemini presets cannot ask for `max` effort.
- 05, 06, 08: a workspace can store its own OpenAI, Gemini and OpenRouter keys beside its Anthropic key. `PUT/DELETE /workspaces/{wid}/provider-keys/{provider}` takes the new provider names, and `provider_keys.provider` allows them (migration 0030; downgrading destroys those keys). Each key is checked against the provider's official API. An OpenRouter key that can only manage keys is refused.
- 07, 22: the operator settings `OPENAI_API_KEY`, `GEMINI_API_KEY` and `OPENROUTER_API_KEY`, and `OPENAI_BASE_URL` (https only, for the operator's OpenAI key; workspaces' own keys always use the official API). Both deployment copies of Compose and `.env.example` gain them, and `catalog/README.md` describes the new providers' catalog entries.

## [0.5.32] - 2026-11-20

### Added

- 22: Compose mounts the model catalog. `deployment/catalog/` (or `MODEL_CATALOG_HOST_DIR`) is bound read-only into the API and the worker at `/etc/codehover/catalog`, so a catalog copied there is used by setting `MODEL_CATALOG_FILE=/etc/codehover/catalog/model-catalog.json`, with no override file. A folder is mounted rather than a file, so a missing file never turns into an empty directory. Both deployment copies gain the mount, the `.env.example` entry and `catalog/README.md`; catalog files in that folder are not committed.

## [0.5.31] - 2026-11-20

### Added

- 07, 22: the optional operator setting `ANTHROPIC_BASE_URL`, an https endpoint for calls with `ANTHROPIC_API_KEY`, such as the operator's own gateway or proxy. It may not contain credentials, a query or a fragment, and blank means the official API. Both deployment copies of Compose and `.env.example` gain it.

### Security

- 07, 08: every Anthropic client is now created with an explicit endpoint. Before, the SDK fell back to the `ANTHROPIC_BASE_URL` environment variable or a local credentials profile, so a backend started outside Compose with that variable set would have sent model calls, including workspaces' own keys, to that endpoint. Workspaces' own keys are now verified and used only against the official API, whatever the operator configures.

## [0.5.30] - 2026-11-20

### Added

- 01, 02, 05, 06, 08, 09, 21 (FR-029): public sites of public-source repositories need a license that permits redistribution (migration 0029). This settles the open question in 21.
  - **The check.** Publishing a public site of a repository read through the public source (FR-028) asks GitHub for the license at the build's exact commit. The SPDX ID must be on the permitted list of open-source licenses (`app/modules/projects/licenses.py`), and the license file must be part of the build, so the site carries its notice. Otherwise the publish fails with 409 `LICENSE_NOT_PERMITTED` (reason `no_license`, `unrecognized`, `not_permitted` or `license_file_missing`), or with 409 `REPOSITORY_NOT_PUBLIC` when the repository has turned private. Private sites, App-granted repositories and uploads are not checked.
  - **The site.** The publication records `license_spdx` and `license_path`; `GET /publication` returns `license_spdx`, the site info returns `license` {spdx_id, path, file_id}, and the site header names the license and links to the file. The license is audited with `publication.changed`.
  - **Backstop and existing sites.** A trigger refuses a public publication without a recorded license unless the repository is App-granted or uploaded. Existing public sites of public-source repositories were suspended with the new reason `license` until an admin publishes again. One already suspended for billing or by an operator turns into a `license` suspension when billing recovers or the operator reinstates it, so it never returns unchecked.

## [0.5.29] - 2026-11-19

### Added

- 01, 07, 15, 21, 22 (T23): Rust.
  - **Parsing.** `.rs` files are parsed with tree-sitter-rust 0.24.2. Items map onto the existing symbol kinds (no migration), `impl` items are named after the implemented type, and doc comments become docstrings.
  - **References.** rust-analyzer 2026-11-02 resolves them by type in the sandbox's indexer profile, with Rust 1.99.0's library sources for std types.
  - **No cargo.** The sandboxed worker builds a `rust-project.json` from the repository's Cargo manifests, so cargo, build scripts and proc macros never run, and nothing is downloaded per build.
  - **Sandbox.** The indexer profile now runs exactly one configured program per run (Node or rust-analyzer), and the startup probe runs once per program.
  - **Installation.** `backend/indexers/fetch_rust.py` installs both downloads, pinned by SHA-256, in the backend image and in CI. Both deployment copies of the Dockerfile, Compose and `.env.example` gain `SCIP_RUST_ANALYZER_PATH`; the image also sets `SCIP_RUST_SRC_DIR`.
  - **Open (21).** The open Rust limits are recorded there: crates.io dependencies, proc macros, build-script output, the assumed 64-bit Linux target, no Rust eval fixtures yet.

### Changed

- 23: engine 0.3.0 and parser bundle 0.3.0 (MINOR: Rust files are now selected and parsed, and a new indexer joins the bundle). Caches are invalidated; the replay eval baseline changes only in its version fields.

## [0.5.28] - 2026-11-19

### Added

- 01, 05, 06, 08, 09, 21, 22 (FR-028): public GitHub repositories without the GitHub App (migration 0028).
  - An editor creates a project from `owner/name` or its github.com address; the repository must be public. It sits under the workspace's one `public` connection, addressed by GitHub's numeric ID, and builds, snapshots, freshness and refs work as for App projects. Push rebuilds are refused (no webhooks).
  - Visibility is checked again on every ref resolution and download; a repository that turns private stops building with `repository_not_public`. Private, internal and missing repositories are the same 404 `REPOSITORY_NOT_PUBLIC`.
  - Metadata may use the optional `GITHUB_PUBLIC_TOKEN` (rate limit only); source tarballs always come anonymously from codeload. The App and the public source share one repository row: an App grant takes a public row over, and a public request for an App-granted repository reuses it.
  - The new-project form offers "A public GitHub repository" and starts on it when the workspace has no App connection. Both deployment copies of Compose and `.env.example` gain `GITHUB_PUBLIC_TOKEN`.
  - A repository whose App access was revoked or suspended cannot be re-added as public (409 `CONNECTION_INACTIVE`).
  - 21 records the open decisions on publishing sites of third-party repositories and on sites of repositories that turn private.

## [0.5.27] - 2026-11-19

### Added

- 02, 07, 12, 21, 22 (T23): type-aware references from SCIP indexers.
  - scip-python 0.6.8 and scip-typescript 0.6.0 (all permissive licenses), with Node 24.22.3 in the backend image, installed from an integrity-pinned lockfile without install scripts. Both deployment copies of the Dockerfile, `.dockerignore`, Compose and `.env.example` are updated.
  - They run in a new `indexer` sandbox profile: no network; exec of only Node and its loader; reads of runtimes, packages and the checkout; writes only to the scratch `out`. The worker probes it at startup and fails closed.
  - Resolved references become `index` edges and the viewer says "resolved by type analysis"; a timeout or failure keeps that language on labeled name matching. The manifest counts edges by resolution and records indexer versions.
  - New optional settings `SCIP_NODE_PATH`, `SCIP_TOOLS_DIR`, `SCIP_TIMEOUT_SECONDS` and `SCIP_HEAP_MB`; the worker's Compose memory limit is 3 GB.

### Changed

- 23: engine 0.2.0 and parser bundle 0.2.0 (MINOR: resolved references change ranking and related context). Caches are invalidated; the replay eval baseline changes only in its version fields.

## [0.5.26] - 2026-11-19

### Added

- 01, 02, 05, 06, 09, 21: per-project access control (migration 0027).
  - Admins restrict a project to chosen members, each granted viewer or editor; that grant is their role on the project. Admins and owners always have access, and support access never reaches restricted projects.
  - One SQL rule (`app_private.project_role`) serves the API, the event stream, members-only sites and a restrictive row policy on `projects`. Leaving the workspace removes a member's grants.
  - The project page has an Access panel, and the project list marks restricted projects.

## [0.5.25] - 2026-11-19

### Added

- 01, 05, 06, 07, 09 (M6, T21): push rebuilds.
  - Each project has a push policy (off, snapshot or build) and a push cap, set by an admin with an ETag. Work runs on that admin's behalf, and the policy turns off if they lose the role.
  - Push webhooks are debounced into one request per project, the latest commit wins, and a commit is never built twice.
  - Generated projects reuse their approved configuration within the cap; others get a replay build. Nothing a push starts is published. Migration 0024.
  - The project page has a "Rebuild on push" panel with the push history. New optional settings are `PUSH_DEBOUNCE_SECONDS` and `PUSH_MAX_WAIT_SECONDS`, added to both deployment copies.
- 05, 06, 07, 08, 11 (M6, T24): bring your own key and batch processing (migration 0026).
  - Owners save one Anthropic key per workspace. It is checked with the provider, sealed and shown only as a hint. Admins and owners can start builds with `funding: own_key`, which spends within the cap and never uses the plan allowance; a removed key stops the build.
  - `batch: true` sends explain requests as one provider batch at the catalog batch price. The batch ID is persisted, the task waits without spending attempts, and anything the batch could not answer runs synchronously at the standard price.
  - Presets report `batch` and `own_key`, and both copies of the catalog example have a batch price.
- 21: SCIP (T23) is deferred by decision, with the review it needs recorded.
- 01, 02, 06, 07, 19 (M6, T22): language and level variants (en/de/id × standard/beginner/expert) and prompt set 0.4.0 with the audience instruction.
  - A variant reuses its base build's snapshot, scope and guide plan, so page and source anchors are identical, and its citations are validated against the same snapshot. The decision to write from evidence rather than translate is recorded in 19.
  - `GET /builds/{bid}/variants` serves the build header's "Read in" switcher and "Make a variant". Exports name their variant.
- 05, 06, 08 (M6): ZIP repository import.
  - A project can come from an uploaded ZIP (`source: upload`), held under the workspace's one upload connection (migration 0025).
  - Each upload is a content-addressed snapshot whose commit identifier is the archive's SHA-256. It is extracted in the worker by `extract_zip` with the tarball's limits, counting inflated bytes.
  - Uploaded projects build their latest scanned upload and work with estimates, generation, publication and export like GitHub projects. The workspace page offers "An uploaded ZIP archive" as a source.

### Changed

- 22: the gateway allows 101 MiB on `/api/v1/projects/{pid}/uploads` only. Other routes keep 2 MiB. Both copies of `nginx.conf` are updated.
- 19, 23: prompt set 0.4.0 (MINOR: the audience text). English/standard prompts read as in 0.3.0, the replay eval baseline changes only in its digest, and caches are invalidated.

## [0.5.24] - 2026-11-18

### Added

- 05, 06, 11 (M5): Stripe Checkout and the Customer Portal, owner-only, with prices mapped from operator settings and a 20-per-hour limit. Billing state comes from reconciled `subscriptions` (migration 0022). Webhooks are verified, recorded as receipts that keep identifiers only, and processed by the worker, which always fetches the customer's subscriptions from the provider. There is a daily reconciliation, plus `python -m app.ops.billing reconcile|process`.
- 11: the plan lifecycle. Past due and open disputes block generation (402). Past due beyond 7 days, and (with `BILLING_ENFORCED`) cancelled beyond 30 days, suspend hosting with reason `billing`, lifted on recovery, never deleting data. Cancel at period end keeps everything until the period ends. Disputes are tracked one by one; after a lost dispute generation stays blocked until an operator runs `app.ops.billing clear-dispute`. Republishing cannot lift a billing suspension (409 `SUSPENDED_FOR_BILLING`). A workspace with a subscription cannot be deleted.
- 11: the one-time trial (`app_private.start_trial`), once per workspace and per person (migration 0023). Plan allowances and active-build limits apply; with `BILLING_ENFORCED`, so do project and seat limits on new writes.
- 02, 06: the Plan and usage page (S16) with a waiting state after checkout (A20). The start page shows the plans. Privacy, terms and support pages are marked as drafts for legal review (S22), fed by the public `GET /meta`.
- 22: billing configuration, billing incident and recovery procedures, and the accounting review (three separate records: provider, entitlements, ledger).
- 13: the payment failure matrix (`tests/integration/test_billing.py`) and an E2E journey with axe over the trial and the legal pages.

### Changed

- 06: Stripe webhooks are verified by the product's own adapter with Stripe's documented signature scheme, not the SDK, so tests replay the provider through `httpx.MockTransport`.
- 22: new settings `STRIPE_PRICE_PRO`, `STRIPE_PRICE_TEAM`, `BILLING_ENFORCED`, `OPERATOR_NAME`, `SUPPORT_EMAIL` and `RATE_LIMIT_BILLING_PER_HOUR`, all optional.

### Fixed

- 22: a blank optional setting in the env file (`KEY=`, as the template ships) now means "not configured". Before, `ANTHROPIC_API_KEY=` or `STRIPE_SECRET_KEY=` counted as an empty credential.
- 22: `deployment/.env.example` in the package again matches the live copy: the Prometheus and trusted-proxy settings were missing.

## [0.5.23] - 2026-11-18

### Changed

- 02, 17: the design pass. It adds the owned shadcn/ui-style components on Radix, Lucide icons, self-hosted IBM Plex and the app shell (232 px sidebar, 64 px top bar, mobile drawer). The code viewer gains resizable tree, code and outline panes, syntax highlighting, and a floating hover card anchored at the identifier, pinned as a non-modal dialog; the offline export viewer gets the same card. Pages use tables, badges and empty states.
- 13: the E2E journey also runs in the dark theme, so axe checks contrast in both themes.

## [0.5.22] - 2026-11-18

### Fixed

- 05: symbol keys are unique per file, not per build (migration 0021). Any repository that defined the same top-level name in two files (`main`, `run`) failed to index. Found by the local demo on a real package.
- 07: a stage whose database writes fail is now a transient task failure, retried within the attempt budget. Before, it left the task leased until the reconciler noticed.

### Added

- 22: a local demo (`python -m tests.e2e.demo`), with fake Google and GitHub, its own database and bucket, and two demo repositories.

## [0.5.21] - 2026-11-18

### Added

- 12, 13, 22: the synthetic probe and load check (`app.ops.loadcheck`), restore verification (`app.ops.verify_restore`) and `tombstones purge`, the local restore drill (`tests.e2e.restore_drill`), and the drilled restore order in the runbook.

### Security

- 05: runtime privileges now come from migrations alone (0020). Purge needed `DELETE` on exports and claim checks, granted only by the bootstrap defaults. The defaults also gave the runtime table-wide `UPDATE` on support grants (enough to extend one), `DELETE` on provider attempts and webhook receipts, and `UPDATE` on search documents; 0020 revokes those. Found by the restore drill.

## [0.5.20] - 2026-11-18

### Added

- 13: M4 threat tests: the cross-tenant and anonymous sweep over every route of the OpenAPI contract, and the XSS corpus for the viewer, the guide and the offline export viewer.

### Security

- 06: the API sets `X-Content-Type-Options: nosniff` on every response itself, beside the gateway's headers.

## [0.5.19] - 2026-11-18

### Security

- 06: rate-limit hits now run on a dedicated small connection pool. On the request pool, each authenticated request held one connection while waiting for a second, so a full pool could deadlock against itself.
- 06: IPv6 clients are counted per /64, so rotating addresses within one allocation no longer escapes the per-address limits; IPv4-mapped addresses count as IPv4.

Both were found by automated security review of 0.5.18.

## [0.5.18] - 2026-11-18

### Added

- 06, 22: rate limits as implemented (migration 0019). Fixed windows are counted in PostgreSQL and shared by all replicas, with HMAC buckets, 429 `RATE_LIMITED` and `Retry-After`. The spec's policies for users, sign-in starts, site delivery, estimates, acquisitions and build starts are now enforced. `TRUSTED_PROXIES` controls which peers' `X-Forwarded-For` is believed; it is in Compose for both copies. Retention removes windows older than two days.

## [0.5.17] - 2026-11-18

### Added

- 02, 05, 06, 08, 09, 11, 22: platform operations (migration 0018):
  - an operations role granted by the deployment, with every operator action re-checked by a database function and recorded in an operator log and the affected workspace's audit log;
  - the `/ops` view and endpoints: aggregates, build diagnostics, the new-build kill switch (503 `BUILDS_PAUSED`), site takedown and reinstatement, and manual entitlement grants that raise the period allowance;
  - support access granted by workspace admins: read-only, at most 72 hours, revocable, with each reading session audited;
  - `GET /me` gains `operator`.

### Security

- 05: the local bootstrap's default privileges give the runtime role full rights on every new table. Migration 0018 therefore revokes them on the operations tables, keeping read access to the platform switch only. It also revokes runtime writes to entitlements, deleting support grants, and deleting deletion tombstones (0015). Without this, the runtime role could have made a user an operator. Found by the schema-wide tenancy guard while writing 0018, before any commit.

## [0.5.16] - 2026-11-18

### Added

- 12, 22: metrics as implemented in T18. Each process serves Prometheus metrics with bounded labels on an internal port, and the scheduler refreshes aggregate job-system gauges from `app_private.ops_gauges` (migration 0017).
- 12, 22: reference scrape configuration and alert rules with promtool unit tests in `deployment/prometheus/` (both copies), plus an `ops` Compose profile with a digest-pinned Prometheus and `METRICS_PORT`.
- 13: the A24 telemetry check.

### Security

- 05: the retention sweep re-checks the publication pointer in a separate statement after locking the build. A check inside the locking statement keeps its old snapshot under READ COMMITTED, so a publish committed while the sweep waited could have been purged. Found by automated security review of 0.5.15; a lock-wait race test covers it.

## [0.5.15] - 2026-11-18

### Added

- 05, 06, 08: retention sweeps as implemented in T17 (migration 0016). Exports gain the `expired` state, builds gain `content_purged_at`, and a purged build cannot be published or exported (409 `BUILD_EXPIRED`). Identity and audit rows are swept through functions whose policies admit only rows past their window; the scheduler removes dispatched outbox rows after 7 days.

### Security

- 05: an operator takedown survives project deletion. The purge keeps the suspended publication row, so its public address stays reserved, and the 0012 trigger again refuses deleting it in every case. Found by automated security review of 0.5.14.

## [0.5.14] - 2026-11-18

### Added

- 05, 06, 08: project, workspace and account deletion as implemented in T17 (migration 0015). This adds `deletion_requests` tombstones, with a copy in object storage, and fencing of running work. A purge removes content and objects once fenced leases have expired. The account data export is added too.
- 06: `POST /workspaces/{wid}/deletion` (owner) and the `/me/export` and `/me/deletion` contracts, including the sole-owner rule.
- 22: `python -m app.ops.tombstones reapply` in the restore procedure.
- 13: the journey's delete step.

### Changed

- 05: project and workspace slugs are unique among live rows only, so a deleted address can be reused.
- 02: S18 starts at `/account` with export and deletion.

## [0.5.13] - 2026-11-18

### Added

- 13: the critical journey as automated for M3, with axe on every screen; project deletion joins it with purge in M4.

### Changed

- 02: a definition's trigger is its identifier, not the whole line, so visible text is part of the accessible name (WCAG 2.5.3), in the viewer and the offline export.

## [0.5.12] - 2026-11-18

### Added

- 06, 05: project freshness (`GET /projects/{pid}/freshness`, `projects.last_observed_sha/at`): a 5-minute observation cache, and `unknown` with the last observation when GitHub is unreachable (A23). The workspace audit log adds an action-prefix filter and filter-bound cursors.

## [0.5.11] - 2026-11-18

### Added

- `contracts/kb-export.schema.json` 1.0.0: the knowledge-base export.
- 05, 06, 10: exports as implemented in T16: the `exports` table, the request, status and download contracts with requester-or-admin access, the 24-hour expiry, the hourly quota, and the fixed-entry offline ZIP.

## [0.5.10] - 2026-11-18

### Security

- 06: an admin could unpublish an operator-suspended publication, which deleted the takedown with the row, and then publish afresh. Unpublishing such a publication is now refused, by the service and by a database trigger (migration 0012). Found by automated security review of 0.5.9.

## [0.5.9] - 2026-11-18

### Security

- 06, 05: three findings of the automated review of T15, fixed with tests.
  - The resolver ignored workspace status and project deletion. It now serves only live projects in active workspaces, and the members-only check requires an active workspace (migration 0011).
  - Republishing cleared an operator's suspension. It is now refused with `SUSPENDED_BY_OPERATOR`.
  - A revoke committed concurrently with a publish could leave an unsuspended pointer. The publish now reads the repository and connection FOR SHARE.

## [0.5.8] - 2026-11-18

### Added

- 05, 06, 09: publication as implemented in T15:
  - the pointer, with the review gate, partial acknowledgment, ETag concurrency, typed public confirmation and rollback;
  - members-only private site addresses;
  - suspension triggers on revoked connections and repositories;
  - published-site delivery through the resolver on every request;
  - the error codes `PUBLICATION_BLOCKED`, `ACKNOWLEDGMENT_REQUIRED`, `CONFIRMATION_REQUIRED` and `SLUG_INVALID`.

## [0.5.7] - 2026-11-18

### Added

- 05, 06: build-scoped search (`search_documents`, `GET /builds/{bid}/search`) with ranking by exact name, name prefix and weighted text, and relations carrying file IDs and lines.
- 02: the M3 code view: an accessible line renderer with deep links, hover preview, pin, Esc and focus return, the build tabs (Overview, Code, Guide, Search, Review), and admin waivers in Review. Adopting CodeMirror 6 is recorded as open.

## [0.5.6] - 2026-11-18

### Security

- 06: an event-stream slot was released only by the response body's generator, which never runs when the client disconnects before the first byte. Repeated aborted requests could leak slots until a workspace's members could no longer stream. Release is now idempotent and tied to the response lifecycle. Found by automated security review of T13.

## [0.5.5] - 2026-11-18

### Added

- 06: the build event stream as implemented in T13, negotiated on `Accept` beside the JSON polling form, with the `access.revoked` and `reset` events and the `STREAM_LIMIT` error.

## [0.5.4] - 2026-11-18

### Added

- 07: the evaluation corpus, labels, harness and committed replay baseline as implemented in M2, with their hard gates and their provisional status until two maintainers label them.

### Changed

- 07, 23: a preset may not pair a model writer with the replay checker, because the replay checker only matches identifiers and the baseline shows it labels unsupported claims as supported.

## [0.5.3] - 2026-11-18

### Changed

- 06: cancellation, retry and build-event polling as implemented in M2: immediate stop when nothing runs, no new earmark after a cancel request, cancellation at the next stage boundary, `retry_of_id`, `ESTIMATE_MISMATCH`, and `{items,next_after}` polling ahead of the SSE stream.

## [0.5.2] - 2026-11-18

### Added

- `contracts/claim-check.schema.json` and `claim-repair.schema.json` 1.0.0, the outputs of P05 and P06 (the files first shipped in 0.5.1 ahead of their implementation).
- 05: the full `claim_checks` row and `guide_pages.original_markdown_key`.
- 06: the quality summary and check list, waiver rules (`WAIVER_NOT_ALLOWED`), and claims on guide pages.

### Changed

- 07: verification as implemented in T12: the verify and check stages, checker batching and isolation, `unchecked` on any checker failure, the bounded repair cycle with results carried over for unchanged claims, and the `uncited_pages` blocker.
- 19: prompt set 0.3.0 adds P05 and P06.

## [0.5.1] - 2026-11-18

### Security

- 07: related context for explanations included definitions (name, kind, signature, path) from files outside the approved scope, which would be sent to the provider. It is now limited to references whose both ends are in scope; an integration test captures every prompt. Found by automated security review of T11.
- 19: the guide Markdown allowlist used regular expressions, which can disagree with a renderer (for example reference-style `javascript:` links). It now checks CommonMark tokens. Found by automated security review of T11.

## [0.5.0] - 2026-11-18

### Added

- `contracts/overview.schema.json`, `guide-plan.schema.json` and `guide-page.schema.json` 1.0.0: the structured outputs of P03, P01 and P04.
- `deployment/model-catalog.example.json`: an operator catalog template with one Anthropic preset, whose region and retention entries must be confirmed before use.
- 05: `generation_cache` (tenant-local cache and stage checkpoint), `file_explanations`, and the full `guide_pages` columns.
- 06: the guide index and page responses, file explanations in the file view, and 422 `PRESET_UNAVAILABLE`.

### Changed

- 07: the provider gateway as implemented in T11: the adapters, error classes, earmark bound, stage list, at most four units in flight, cache checkpoints, labeled fallbacks, and unknown outcomes on re-claim.
- 19: prompt set 0.2.0, its data-boundary markers and the repair template.

### Removed

- 22: the `MODEL_PROVIDER`, `MODEL_ID` and `MODEL_API_KEY` settings, replaced by `MODEL_CATALOG_FILE` and per-provider credentials (`ANTHROPIC_API_KEY`).

## [0.4.2] - 2026-11-18

### Security

- 07, 06: the bounded glob matcher of 0.4.1 was still polynomial in tenant-controlled paths and globs; a probe of the committed code took 61 s for one selection. Selection now has a fixed work budget (422 `SCOPE_TOO_COMPLEX`), segments match literal pieces with `str.find` and are memoized, `?` is no longer accepted, globs are limited to 20 per list with at most 8 `*` and 16 segments, and the estimate runs off the API event loop. Found by automated security review of 0.4.1.

## [0.4.1] - 2026-11-18

### Security

- 07: scope globs are matched by a bounded segment matcher instead of `PurePath.full_match`, whose regular-expression translation took exponential time on globs such as `*a*a*a*a*b`; a single estimate request could pin an API worker. Character classes are no longer accepted. Found by automated security review of T10.
- 06: snapshot acquisitions are admitted under the usage-account lock, at most `MAX_ACTIVE_ACQUISITIONS_PER_WORKSPACE` at once (409 `ACQUISITION_LIMIT`). The build limit did not cover them, so a workspace could start unbounded archive downloads and scans. Found by automated security review of T10.

## [0.4.0] - 2026-11-18

### Changed

- `contracts/task-envelope.schema.json` 2.0.0 (incompatible): a task names exactly one build or one snapshot, because a snapshot is now acquired and scanned before any build exists. Consumers still accept v1 messages while they drain (23).
- 05: `build_tasks` may belong to a snapshot; `builds` record the usage account their envelope was reserved against; `provider_attempts` gain the build, fencing generation, role, purpose, model and output digest; `usage_entries` amounts are non-negative, with the direction carried by the kind.
- 06: the snapshot, estimate, build start, usage and catalog-preset contracts as implemented in T10, with the error codes `SNAPSHOT_NOT_READY`, `PRESET_UNKNOWN`, `SCOPE_EMPTY`, `ESTIMATE_STALE`, `ALLOWANCE_EXCEEDED` and `WORKSPACE_BUILD_LIMIT`.
- 07: the estimate runs before parsing, so scope selection is one deterministic file-level function shared by the estimate and the build.
- 11: until entitlements exist, the monthly allowance is the operator setting `GENERATION_ALLOWANCE_MICRO_USD` (default 0).

## [0.3.12] - 2026-11-18

### Security

- 22: the parser sandbox adds a Landlock filesystem allowlist, denies process inspection (`ptrace`, `process_vm_*`) and program execution, and the worker becomes non-dumpable. A compromised parser could otherwise read credentials as the same UID and leak them through parse output. Found by automated security review. While implementing this, a jump-offset error in the rewritten seccomp program briefly allowed every denied syscall; the Linux probe caught it, and a unit test now pins the jump targets.

## [0.3.11] - 2026-11-18

### Changed

- M1 implementation decisions recorded:
  - 22: the sandbox design (seccomp bootstrap, worker self-check, Linux CI gate) and the dead-letter queue `codehover.dead`;
  - 07: the replay generator and the M1 stages;
  - 06: replay build start by `ref` until estimates arrive in M2;
  - 05: the webhook status function and re-notification via `notified_at`.

## [0.3.10] - 2026-11-18

### Security

- 09: introducing repository grants fails closed. Rows that existed before migration 0004, possibly with over-broad grants, start revoked until a reconnect. Found by automated security review.

## [0.3.9] - 2026-11-18

### Security

- 09, 05, 06: a GitHub connection grants only repositories the connecting user can read, with a known re-verification limitation. Previously anyone able to read one repository of an installation could expose all of its repositories to their workspace. Found by automated security review of T05; implemented by migration 0004.

## [0.3.8] - 2026-11-18

### Changed

- 06, 09, 22: the GitHub App connect flow as implemented in T05: the two-phase setup and callback, the required linked GitHub identity, the role re-check, the App URL and permission settings, the repository and ref contract, and the error codes.

## [0.3.7] - 2026-11-18

### Added

- 05: `invitation_by_token`, and the database-enforced owner invariant (deferred, workspace-locking trigger `memberships_keep_owner`). Implemented by migration 0003 (T04).
- 06: workspace, member and invitation contract details: ETag preconditions, invitation `accept_url` with the token in the fragment, role limits per action, and error codes.

### Changed

- 05: queries must filter by `workspace_id` explicitly even inside a workspace context, because user-scoped policies widen visibility. Found when an unfiltered member list leaked memberships across workspaces during T04.

## [0.3.6] - 2026-11-17

### Security

- 09 and 05: every OAuth flow is bound to the browser that started it, through a flow cookie whose digest consumption requires. This closes login CSRF: a callback URL opened in another browser can no longer sign the victim into the attacker's account, nor burn the attacker's transaction. Found by automated security review of T03.

## [0.3.5] - 2026-11-17

### Changed

- Decisions made while implementing sign-in (T03) are now in the spec:
  - 09: the CSRF derivation, the exact-Origin rule, the session touch interval, reauthentication by signing in again, and the development cookie name;
  - 06: the `return_to` parameter, callback error redirects, authentication error codes, and `no-store` on API responses;
  - 05: sealed PKCE verifier and nonce storage.

## [0.3.4] - 2026-11-17

### Added

- 05 adds the `idempotency_keys` table: 06 required `Idempotency-Key` storage, but the data model had no table for it.
- 05 adds the pre-authentication lookups `identity_user`, `create_user_with_identity`, `session_by_token` and `oauth_transaction_create/consume`, with their RLS rules. These were found while implementing migration 0002 (T02).

## [0.3.3] - 2026-11-17

### Added

- Implementations record the package version they conform to in a root `SPEC_VERSION` file. CI validates it against the package changelog, and images carry it as the label `codehover.spec.version`. This is defined in 23 and reflected in 16 and 18; the `Dockerfile.*` templates gain the `SPEC_VERSION` build argument and label.

## [0.3.2] - 2026-11-17

### Added

- Host-port overrides in the Compose templates (`DB_HOST_PORT`, `AMQP_HOST_PORT`, `RABBITMQ_MANAGEMENT_HOST_PORT`, `S3_HOST_PORT`, `WEB_HOST_PORT`), with unchanged defaults, so the stack starts next to a local PostgreSQL or RabbitMQ.

### Changed

- `VALIDATION_REPORT.md` records the first real run of the container templates: image builds, Compose start-up, the gateway, SeaweedFS and RabbitMQ, using the M0 implementation.

## [0.3.1] - 2026-11-17

### Changed

- The local object store is decided: SeaweedFS 4.52 (`weed mini`), pinned by digest. This replaces the archived MinIO image in the `deployment/` templates, 21 and 22.
- Deployment templates gain:
  - a `bootstrap` Compose service, with the dead-letter exchange `codehover.dlx` and the `codehover.work.*` queue policy;
  - `APP_ENV` and `RABBITMQ_MANAGEMENT_URL` settings;
  - OCI version labels on images;
  - pip installs from the hash-pinned lock with `--no-deps`.

  The templates now match the implemented repository copy from M0.

## [0.3.0] - 2026-11-17

### Added

- `23_VERSIONING_AND_RELEASES.md`: one SemVer-based registry, with a 0.y.z initial-development rule (incompatible changes bump MINOR until 1.0.0). It covers the package, application releases, the HTTP API, JSON contracts, the queue envelope, stored artifacts, the engine, parser and prompt sets, model presets, price catalogs and database migrations. It also sets compatibility, deprecation and cache rules and the release process.
- `VERSION` as the package's single hand-edited version; `tools/assemble.py bump` and stricter `--check` (SemVer format, changelog order, version agreement across generated files, contract `$id` consistency).
- `$id` URNs with SemVer on `contracts/explanation.schema.json` (2.0.0) and `contracts/task-envelope.schema.json` (1.0.0).

### Changed

- 05, 06, 07, 10, 16, 18 and 19 now point to the version registry. The prompt directory is `prompts/<X.Y.Z>/`, the KB export identifies itself by schema `$id`, and `/health/live` reports the application version.

## [0.2.0] - 2026-11-17

Review revision, first circulated as "1.1" and briefly as "2.0.0". It changes contracts incompatibly, which under the 0.y.z rule is a MINOR bump:
- explanation schema v1 → v2;
- `DELETE /projects/{pid}/publication` takes `If-Match` instead of a body;
- new required settings `APP_ENCRYPTION_KEY` and `APP_SYSTEM_DB_PASSWORD`;
- the database role model changed.

The 0.1.0 package was reviewed for correctness and internal consistency. Its reference SQL was then executed on PostgreSQL 18.0. Fixes are grouped by severity. Numbers refer to the documents changed.

### Design gaps that would have blocked M1

- **FORCE RLS made the control loops and flat routes impossible (04, 05, 22, `contracts/`, `deployment/`).** With every tenant table under FORCE RLS, three things failed:
  - the dispatcher, scheduler and reconciler could not see any outbox or task rows;
  - `/projects/{pid}`-style routes could not find their workspace before setting the context;
  - `GET /workspaces` could not list a user's own workspaces, and new workspaces could not be inserted at all.

  Added:
  - an `app_system` role limited to the outbox and task-scheduling columns;
  - a NOLOGIN `app_resolver` role owning SECURITY DEFINER lookups (`resource_workspace`, `resolve_publication`, `create_workspace`);
  - user-scoped SELECT policies via `app.user_id`.

  No role uses BYPASSRLS. Bootstrap and compose now create and use these roles.
- **Installation verification could not work as specified (06, 09, 22, `.env.example`, `compose.yaml`).** `GET /user/installations` requires a user access token from the GitHub App itself. The identity OAuth app's token cannot prove a user's access to an installation. The setup flow now gets a short-lived GitHub App user token, and the App's client ID and secret are added to the settings.
- **The explanation schema was incompatible with strict structured outputs (`contracts/explanation.schema.json`, 05, 06, 19).** `params` was an open map (`additionalProperties` as a schema), which strict modes reject. It is now an array of `{name, description}` (schema v2). Length and count keywords are enforced by the local validator, not sent to providers that reject them.
- **The parser sandbox was an M1 gate in 22 but missing from the roadmap and backlog (14, 15).** It is now part of M1 deliverables and exit criteria, and of T08.

### Correctness and security fixes

- **nginx (`deployment/nginx.conf`, 08, 22):**
  - Security headers were silently dropped on `/assets/`, because a location with its own `add_header` stops inheriting the server-level ones.
  - Added the baseline CSP that 08 requires but no template set.
  - The SPA shell is now `no-cache`.
  - The one-hour read timeout and disabled buffering now apply only to the SSE route; other API routes get 60s timeouts.
  - Client-supplied `X-Forwarded-*` headers are overwritten instead of passed through.
  - The upstream is re-resolved through Docker DNS.
- **Unchecked claims contradicted build immutability (07, 20 A13).** A13 said publish stays blocked "until execution completes", but terminal builds never re-run. Now a retry build re-runs the checks, reusing cached generation.
- **Concurrency was contradictory (07).** "4 per build and 2 per workspace" could never reach 4. Clarified as 4 concurrent provider calls per build, plus the active-build limits from 01.
- **Column convention was unspecified (02, 05, 06, 07).** Columns are now 0-based and end-exclusive, counted in Unicode scalar values. Tree-sitter UTF-8 byte offsets are converted once at the parse stage.
- **Tenant setting (05).** Use `set_config('app.workspace_id', $1, true)` with bound parameters. `SET LOCAL` cannot take parameters and invites string formatting.
- **API (06).** `DELETE /projects/{pid}/publication` now uses `If-Match` instead of a request body.
- **RabbitMQ 4 (04, 22).** Quorum queues drop messages after 20 deliveries unless a dead-letter exchange is configured. A DLX policy is now required.
- **Missing data-encryption key (22, `.env.example`, `compose.yaml`).** Added `APP_ENCRYPTION_KEY` for encrypted PKCE verifiers and webhook payloads, kept separate from `SESSION_SECRET`.
- **Build context leakage (`Dockerfile.backend`, new `*.dockerignore`, 16).** The repo-root build context would have sent `deployment/.env` and `secrets/` to the builder. Allowlist ignore files are added. Backend code is no longer owned by the runtime user.
- **Accessibility (17).** `border` reaches only about 1.3:1 in light mode. Added an `input-border` token (at least 3:1 on every surface) for control boundaries.
- **Local object store (21, 22, `.env.example`).** MinIO community images stopped in October 2025 and the project was archived. This is recorded as a risk and an M0 decision.

### Consistency

- Build-scoped frontend routes use one prefix, `/p/:pid/builds/:bid/...` (02).
- Claim extraction is now defined deterministically (07). Multi-chunk file summaries are defined (07).
- `core_schema.sql` gains:
  - task state and publication slug CHECKs;
  - a `reversal` ledger kind (05 and 11 already referenced reversal entries);
  - memberships;
  - REVOKE of runtime UPDATE/DELETE on the ledger.
- Added `contracts/core_schema_checks.sql`: executable tenancy, role and integrity assertions, mutation-tested.
- Package manifest: the 0.1.0 manifest checksums were already stale for 7 files and the master spec, despite that release's validation claim. `tools/assemble.py` now regenerates the master spec and manifest, and checks them.

## [0.1.0] - 2026-11-17

Initial planning package (originally labelled "1.0"; renumbered because an unimplemented specification is initial development under SemVer): documents 00–22, reference SQL subset, JSON schemas and container templates.
