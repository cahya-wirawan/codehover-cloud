# Versioning and releases

Every versioned surface in codehover-cloud uses [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html) (`MAJOR.MINOR.PATCH`, optional `-pre.release` and `+build` metadata) unless the table below says otherwise. A version names exactly one immutable thing: once released, its content never changes, and a fix is a new version. Precedence follows SemVer (`1.4.0-rc.2 < 1.4.0 < 1.10.0`); build metadata never affects precedence or compatibility.

## Version registry

| Surface | Scheme and source of truth | MAJOR when | MINOR when | PATCH when |
|---|---|---|---|---|
| Build package (this specification) | SemVer in `VERSION`; manifest, README and master spec are generated from it | A contract file or normative requirement changes incompatibly: removed or renamed endpoint, field, setting, role or command; new **required** setting; schema change rejecting previously valid documents; reversed requirement | Backward-compatible addition: new requirement, document, optional field or setting, check, or clarification that resolves an ambiguity without contradicting earlier text | Editorial only: wording, typos, links, formatting, tooling or checksum fixes; no requirement changes |
| Application release (backend and frontend together) | SemVer git tag `vX.Y.Z` on the release commit; one version for every image built from it; staging candidates `X.Y.Z-rc.N` | The public HTTP API major increases, or an exported artifact format (static ZIP, KB JSON) drops a major | New user-visible capability or optional API addition | Bug or security fix without contract change |
| Public HTTP API | Path carries MAJOR (`/api/v1`); OpenAPI `info.version` carries full SemVer and is generated from code | Any change a conforming client can observe as breaking (see compatibility rules) | Additive change: new endpoint, optional request field, new response field, new value in a field documented as open | Documentation or behavior fix within the documented contract |
| JSON contracts (`contracts/*.schema.json`) | SemVer in the schema's `$id` URN `urn:codehover-cloud:schema:<name>:<X.Y.Z>`; title states `v<MAJOR>` | A previously valid instance becomes invalid, or a field's meaning changes | New optional property or relaxed constraint | Description or `$comment` change only |
| Queue task envelope | Payload field `schema_version` = MAJOR integer of the task-envelope schema | As JSON contracts | Not carried in payload (additive fields are ignored by older consumers) | — |
| Stored build artifacts (manifest, explanation, guide bundle, KB export) | Manifest lists each artifact's schema `$id`; viewer and exporter dispatch on MAJOR | As JSON contracts | As JSON contracts | — |
| Generation engine, parser bundle, prompt set | SemVer each, recorded per build (`builds.engine_version`, `builds.prompt_version`, parser version in the manifest) | Output artifact schema MAJOR changes | Anything that can change generated or deterministic output: prompt text, grammar update, ranking, chunking, citation extraction; requires the evaluation gate in 07 | No change to model inputs or deterministic outputs; replay fixtures must be byte-identical (CI-enforced) |
| Model preset | Immutable name `<name>-v<N>` (for example `balanced-v1`) | Any change to provider, model, parameters or catalog binding creates `v<N+1>`; never edited in place | — | — |
| Price catalog | Immutable dated revision `catalog-YYYY-MM-<letter>` (not SemVer: ordering by date is the meaning) | Any price or rate change creates a new revision | — | — |
| Database schema | Alembic revision chain (not SemVer); each application release records its head revision | Handled by expand/contract migrations (04), not by version numbers | — | — |

## Initial development (0.y.z)

The build package and the application start at `0.1.0`. While MAJOR is 0, SemVer makes no stability promise, so the registry's MAJOR column bumps **MINOR** and both the MINOR and PATCH columns bump **PATCH**. The changelog still labels each change by its class, so `1.0.0` starts from a known baseline.

- **The package** reaches `1.0.0` when an M1 implementation conforms to it: its contract files and acceptance scenarios have been exercised by running code, not only reviewed.
- **The application** reaches `1.0.0` at the first release offered beyond the closed beta (M5 paid launch).
- **Contract schemas** carry their own versions, independent of both (the explanation schema is at 2.0.0 because its v1 shape was replaced).

## Compatibility rules

**HTTP API within a major.** Clients must ignore unknown response fields and unknown values in fields documented as open enums. The server must not:
- remove or rename fields, endpoints or error codes;
- make an optional request field required;
- narrow accepted input;
- change a field's type, unit or meaning;
- change authorization semantics, pagination or idempotency behavior.

Any of these needs a new path major (`/api/v2`), served alongside the old one. In MVP the only client is the bundled frontend, but rolling deployments and cached SPA shells mean API release N must keep serving frontend N−1. That holds automatically if API changes stay additive.

**Deprecation.** A deprecated endpoint or field keeps working for at least one MINOR release and, once external clients exist (P2 hosted MCP), for a published period of at least 90 days (proposed default). Its responses carry `Deprecation` (RFC 9745) and `Sunset` (RFC 8594) headers. Removal happens only in the next MAJOR.

**Messages during deploys.** Consumers accept the current and previous envelope MAJOR. A producer switches to a new MAJOR only after every consumer in the fleet accepts it: two releases, never one.

**Stored artifacts.** Builds are immutable and published builds can live indefinitely, so the viewer and exporter keep readers for every artifact MAJOR still referenced by a retained or published build. Dropping a reader is a release gate: a query must show no retained or published build references that MAJOR. Artifacts are never rewritten in place to a newer schema.

**Caching.** Generation cache keys (07) include engine and prompt-set `MAJOR.MINOR` plus the prompt template digest, so every MINOR invalidates affected entries. PATCH releases keep cache entries valid because their replay fixtures are byte-identical.

**Exports.** The KB export carries its schema `$id`. P1 codehover v2 compatibility is a separate, tested claim, not implied by a version number.

## Release process

1. Changes accumulate under `## [Unreleased]` in the changelog, grouped as Added, Changed, Deprecated, Removed, Fixed and Security ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/) conventions). Every entry that changes a contract names the surface and its new version.
2. The release bump is derived from the largest change in Unreleased, using the registry rules. A CI check classifies changes:
   - OpenAPI diff against the previous release;
   - JSON Schema compatibility for every changed contract;
   - replay-fixture identity for engine PATCH releases.

   It fails a release whose bump is smaller than the classified change.
3. Tag `vX.Y.Z` on the release commit. Images get the version tag and are deployed by digest. Release metadata (18) records the application, OpenAPI, engine, prompt-set and parser versions, the contract `$id`s, model presets, the price catalog and the Alembic head. `GET /health/live` returns the application version.
4. Staging receives `X.Y.Z-rc.N` builds. Production receives only final versions.
5. Hotfixes branch from the released tag and increase PATCH. They never carry features.

## This package

The build package follows the "Build package" row. `VERSION` is the only place its version is written by hand. `python3 tools/assemble.py bump major|minor|patch` moves the Unreleased changelog entries into a dated release section and writes the version into `VERSION`, the README, the master spec and the manifest. The tool is mechanical: choosing the part, including the 0.x rule above, is the author's decision. `python3 tools/assemble.py --check` fails if any of these disagree or the changelog is out of order. It warns when `VALIDATION_REPORT.md` still describes an earlier version, because that report changes only after real re-validation. Package versions are unrelated to application versions. An implementation states which package version it conforms to in a root `SPEC_VERSION` file. CI checks that it names a released package version no newer than `build-package/VERSION`. The spec may run ahead of the code; that is reported, not failed. Release metadata and the image label `codehover.spec.version` carry the value alongside `org.opencontainers.image.version`.
