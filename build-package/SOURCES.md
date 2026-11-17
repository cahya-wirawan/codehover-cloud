# Source provenance

Access date for all sources: 2026-11-17. Repository inspected through authenticated GitHub integration, read-only. Pinned commit `a3f668bc36420b414e7c8e048b06ca5e18ce8ad2`; no source execution. All SaaS architecture, requirements, prices, limits, screens and roadmap are Proposed unless explicitly marked Verified/Strongly inferred.

## Primary reference sources

| Source | URL | Supports |
|---|---|---|
| README | https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/README.md | Documented workflows, UI capabilities, providers, cost controls, variants, MCP/editor integrations |
| Packaging | https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/pyproject.toml | Python dependency declarations and declared MIT license |
| Pipeline | https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/codehover/pipeline.py | Stage orchestration and variant handling |
| Scanner | https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/codehover/scan.py | File selection and source hashing |
| Symbols | https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/codehover/symbols.py | Tree-sitter definition extraction and language mapping |
| Explainer | https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/codehover/explain.py | File prompts, normalization and cache behavior |
| LLM adapter | https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/codehover/llm.py | Request/job abstractions, usage and batch adapters |
| Guide | https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/codehover/wiki.py | Guide planning, evidence ranges and citation cleanup |
| Verifier | https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/codehover/verify.py | Snippet comparison and claim classification |
| Renderer | https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/codehover/render.py | Static-site rendering and symbol/link presentation |
| KB | https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/codehover/kb.py | KB v2 source/symbol/link/guide serialization and lookup API |
| Search | https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/codehover/search.py | BM25 field weighting and relation expansion |
| Freshness | https://github.com/cahya-wirawan/codehover/blob/a3f668bc36420b414e7c8e048b06ca5e18ce8ad2/codehover/freshness.py | File hash changes and commit age reporting |

## Engineering documentation checked

- PostgreSQL 18 UUID functions: https://www.postgresql.org/docs/18/functions-uuid.html — native uuidv7 generation.
- PostgreSQL Docker official image: https://hub.docker.com/_/postgres — image/container storage conventions. Patch tags seen online are not used as a tested production pin.
- SQLAlchemy asyncio: https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html — asynchronous session usage and concurrency boundaries.
- RabbitMQ acknowledgements/confirms: https://www.rabbitmq.com/docs/confirms — publisher and consumer reliability are separate responsibilities.
- RabbitMQ quorum queues: https://www.rabbitmq.com/docs/quorum-queues — replicated queue semantics and production availability topology.
- GitHub OAuth versus Apps: https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/differences-between-github-apps-and-oauth-apps — identity and installation access have distinct grants.
- GitHub App permissions: https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app — least-privilege repository permissions.
- GitHub installation tokens: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation — installation-scoped access.
- Google OIDC: https://developers.google.com/identity/openid-connect/openid-connect — authorization flow and identity-token validation.
- FastAPI containers: https://fastapi.tiangolo.com/deployment/docker/ — container deployment guidance.
- Vite guide: https://vite.dev/guide/ — build/dev setup; exact selected version is locked at implementation.
- Tailwind Vite integration: https://tailwindcss.com/docs/installation/using-vite — frontend integration approach.
- shadcn/ui Vite installation: https://ui.shadcn.com/docs/installation/vite — component setup with Vite.

Authlib candidate documentation URLs attempted during research were unavailable; no behavior was asserted from those failed fetches. Select and verify a supported Authlib release/integration in M0. No regulatory source or market pricing research was used; compliance/pricing sections are proposed checklists/hypotheses awaiting launch review.
