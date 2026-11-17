# Reference analysis and capability mapping

Research date: 2026-11-17. Reference: private `cahya-wirawan/codehover`; pinned main commit `a3f668bc36420b414e7c8e048b06ca5e18ce8ad2`. Inspection covers README, packaging, pipeline, scanner, symbols, LLM adapter, explainer, guide generation, verifier, renderer, KB, search and freshness modules. Observations establish implementation presence, not runtime correctness, security certification, or measured quality.

## Evidence ledger

All file links below refer to the pinned commit and require repository access.

| Capability | Class | Evidence and observation | codehover-cloud disposition |
|---|---|---|---|
| Repository to static website | Verified | [codehover/render.py](https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/codehover/render.py), `SiteBuilder`; [README.md](https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/README.md) describes HTML/CSS/JS output | MVP hosted viewer plus ZIP export |
| Stage-based generation | Verified | [codehover/pipeline.py](https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/codehover/pipeline.py), `Pipeline`; scan/index/rank/explain/guide/check/translate/render | Persist every stage with resumable jobs |
| Definition extraction | Verified | [codehover/symbols.py](https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/codehover/symbols.py), `extract_symbols` at line 130, tree-sitter language mapping | MVP Python/TS/JS; wider languages P1 after fixtures |
| Source selection and hashes | Verified | [codehover/scan.py](https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/codehover/scan.py), `scan_repo` at line 83 | Add hard size limits and safe acquisition |
| Hover explanations and line notes | Verified | [codehover/explain.py](https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/codehover/explain.py), `normalize_file_doc` at line 258; `render.py` | Core MVP code-reading experience |
| Type-aware references with fallback | Verified | [README.md](https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/README.md) code-index section and `kb.py` link serialization label SCIP/index vs name | MVP deterministic syntax/name links, accurately labeled; sandboxed SCIP P1 |
| Related definitions | Verified | `kb.py`, `build_kb` at line 49 serializes uses/used_by | MVP bidirectional relationships |
| Cited topic guide | Verified | `wiki.py`, `build_plan_prompt` line 52, `clean_page_markdown` line 258 | MVP overview plus 3–8 cited guide pages |
| Citation validation | Verified | `verify.py`, `check_snippet` line 131; `CitationChecker` line 191 | Preserve supported/partial/unsupported and add invalid/unchecked |
| Machine-readable KB | Verified | `kb.py`, `KB_VERSION = 2`, `build_kb`, `KnowledgeBase` | Versioned export; compatibility tests before claiming v2 interoperability |
| Ranked contextual search | Verified | `search.py`, `SearchIndex`: BM25, identifier splitting and reference expansion | MVP PostgreSQL lexical search + graph neighbors; no claim of BM25 equivalence |
| Incremental generation and age warnings | Verified | `explain.py` cache; `freshness.py`, `Freshness` compares file hashes | Dependency-aware tenant-local caching and commit freshness |
| Languages and reader levels | Verified | README sections and pipeline variants | English standard MVP; German/Indonesian and beginner/expert P1 |
| API backends, replay, batching | Verified | `llm.py`; README provider and replay sections | One approved provider + replay MVP; batch and BYOK P1 |
| MCP and VS Code | Verified | README integration sections and corresponding repository directories | P1 downloadable KB, P2 authenticated hosted MCP |
| Local-first generation economics | Strongly inferred | CLI/cache/static-output architecture suggests low hosting requirements | SaaS adds ongoing storage, generation and access-control costs |
| Team workspaces, billing and OAuth | Proposed | Not established by inspected reference modules; no SaaS implementation confirmed | New SaaS functionality |
| Current product pricing or adoption | Unknown | Repository does not establish subscriptions, customers, usage or commercial terms | Validate separately |

## Experience inventory

Reference documented entry is CLI install/configure/build/open, with landing overview, file tree, annotated file, hover card, outline, guide pages, citation preview, search, theme and language/level switch. Local static output remains readable without runtime model calls. CLI estimate/update/status and agent/editor integrations are part of the documented ecosystem. No account, checkout, hosted admin or workspace screen was verified. All SaaS screens in this package are Proposed.

Strengths: close connection between explanation and exact source; explicit ambiguity; portable static output; independent citation checks; reader variants; cost-aware incremental processing. SaaS opportunities: remove installation/key setup, make builds observable, support collaboration, maintain publication history, provide privacy controls and predictable spending. These are opportunities, not confirmed defects in the CLI.

## Adaptation boundaries

codehover-cloud is the user-requested product name. Preserve functional concepts while creating a new web interaction design and service architecture. This package does not copy reference code, prompts, assets or branding. The inspected pyproject declares MIT; any later reuse must verify LICENSE and third-party obligations, retain required notices and assess SaaS suitability. A local CLI that may launch indexers must not be exposed directly to untrusted tenants without new isolation.
