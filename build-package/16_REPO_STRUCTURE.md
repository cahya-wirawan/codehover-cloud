# Proposed repository structure and ownership

Repository name: `codehover-cloud`. This package does not create or modify a GitHub repository.

| Path | Responsibility |
|---|---|
| `VERSION` | Application SemVer for the release commit (document 23); backend `pyproject.toml`, frontend `package.json` and image tags are derived from it in CI |
| `backend/pyproject.toml`, `requirements.lock` | Exact Python dependencies and installation contract |
| `backend/app/main.py` | FastAPI factory, middleware and routers |
| `backend/app/core/` | Settings, DB transaction scope, policy, crypto, error envelope |
| `backend/app/modules/{identity,workspaces,github,projects,builds,publications,billing,usage,audit}/` | Models, schemas, services, routers per domain |
| `backend/app/workers/{main,dispatcher,scheduler,reconcile}.py` | Rabbit consumer/outbox/retry/reconciliation commands |
| `backend/app/engine/{scan,parse,rank,explain,guide,verify,render,export}/` | Pure versioned pipeline contracts and bounded tasks |
| `backend/app/adapters/{github,models,storage,billing,email}/` | Replaceable integrations, replay fakes |
| `backend/alembic/versions/` | Full explicit schema/RLS migrations |
| `backend/tests/{unit,integration,security,fixtures}/` | Synthetic source/replay and fault tests |
| `frontend/src/app/` | Router, auth boundary, providers, workspace context |
| `frontend/src/features/{projects,builds,viewer,guide,search,usage,settings}/` | Screen logic and query keys |
| `frontend/src/components/ui/` | Owned shadcn/ui components and accessibility wrappers |
| `frontend/src/components/code/` | CodeMirror span conversion, symbols, pinned popover, outline |
| `frontend/src/lib/{api,schemas,format}/` | Generated typed API, shared schema validation and formatters |
| `frontend/src/styles/tokens.css` | Light/dark semantic design tokens |
| `frontend/e2e/` | Playwright end-to-end journeys |
| `contracts/` | Generated OpenAPI (`info.version` SemVer), JSON schemas with SemVer `$id` URNs, versioned artifact schemas |
| `deployment/` | Dockerfiles with `<Dockerfile>.dockerignore` allowlists (build context is the repo root), Compose, nginx, broker config and runbooks |
| `.gitignore` | Must cover `deployment/.env`, `deployment/secrets/`, `.venv/`, `node_modules/`, `dist/` |
| `docs/` | PRD, architecture decisions, threat model, model eval reports |
| `.github/workflows/` | Lint/types/unit/integration/eval/build/SBOM/image scan/release |

Frontend never imports backend implementation or contains provider secrets. Engine has no HTTP-session concepts and never derives workspace from source content. Adapters return typed errors; domain services enforce authorization/budget/state. Worker trusts SQL task record, not arbitrary message command strings. Backend models are mapped to explicit response schemas to avoid leaking token/object internals. Use structured logs, request IDs and one error-envelope module.

Recommended dependency candidates: FastAPI, Uvicorn, Pydantic settings, SQLAlchemy 2.x, psycopg[binary,pool], Alembic, aio-pika, httpx, Authlib, cryptography, boto3, tree-sitter/language pack, Pygments, markdown-it-py, reviewed sanitizer, stripe; frontend React/Vite/TypeScript/Tailwind/shadcn, TanStack Query, React Router, React Hook Form, Zod, CodeMirror, Lucide, Mermaid strict renderer. Resolve supported exact patch releases in M0 and commit locks; this list is not an untested pinned compatibility promise.
