"""Type-aware references from SCIP indexers (T23), started by the worker's index stage.

For each language family with selected files, the worker writes those files (and nothing else from the
repository) into a fresh scratch checkout, then runs app.engine.index_worker in the sandbox's indexer
profile. Any failure (timeout, crash, unusable index, missing tools) leaves that family on name matching,
labeled `name` as before (07, A12); one indexer failing never fails the build.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from app.engine import sandbox
from app.engine.index_worker import OPENSSL_CONFIG

FAMILIES = {
    "scip-python": frozenset({"python"}),
    "scip-typescript": frozenset({"typescript", "tsx", "javascript"}),
}
PACKAGES = {"scip-python": "@sourcegraph/scip-python", "scip-typescript": "@sourcegraph/scip-typescript"}
SANDBOX_GRACE_SECONDS = 60  # the sandbox's own wall clock outlasts the indexer timeout inside it


@dataclass(frozen=True)
class Config:
    node: str
    tools: str
    timeout: int = 300
    heap_mb: int = 1536
    scratch: str | None = None  # parent of the scratch checkouts; the system temp directory by default


@dataclass(frozen=True)
class Outcome:
    indexer: str
    files: int
    status: str  # "ok" or "failed"
    reason: str | None = None
    version: str | None = None


def versions(config: Config) -> dict[str, str]:
    """Installed indexer versions, recorded in the build manifest."""
    found = {}
    for indexer, package in PACKAGES.items():
        manifest = Path(config.tools) / "node_modules" / package / "package.json"
        try:
            found[indexer] = str(json.loads(manifest.read_text())["version"])
        except (OSError, ValueError, KeyError):
            continue
    return found


def installed(config: Config, indexer: str) -> str | None:
    """The indexer's version when it and Node are present, else None."""
    return versions(config).get(indexer) if Path(config.node).is_file() else None


def tsconfig(paths: list[str]) -> dict[str, Any]:
    """The only TypeScript configuration the indexer sees: the repository's own tsconfig, plugins and
    `extends` chains are never read. Imports resolve between the selected files only."""
    return {
        "compilerOptions": {
            "allowJs": True,
            "checkJs": False,
            "noEmit": True,
            "skipLibCheck": True,
            "allowImportingTsExtensions": True,
            "types": [],
            "target": "es2022",
            "module": "preserve",
            "moduleResolution": "bundler",
            "jsx": "preserve",
            "maxNodeModuleJsDepth": 0,
        },
        "files": paths,
    }


MAX_PYTHON_ROOTS = 100


def python_roots(paths: list[str]) -> list[str]:
    """Import roots for scip-python: the directory above each file's outermost package (a directory with
    `__init__.py`), so `backend/app/x.py` imports as `app.x` as it does in the repository. Like the
    tsconfig, this is the only configuration the indexer sees."""
    packages = {str(PurePosixPath(p).parent) for p in paths if PurePosixPath(p).name == "__init__.py"}
    roots = set()
    for path in paths:
        directory = PurePosixPath(path).parent
        while str(directory) in packages and str(directory) != ".":
            directory = directory.parent
        roots.add(str(directory))
    return sorted(roots - {"."})[:MAX_PYTHON_ROOTS]


def _safe(path: str) -> bool:
    pure = PurePosixPath(path)
    return bool(path) and not pure.is_absolute() and ".." not in pure.parts and "\\" not in path


def _scratch(work: Path) -> None:
    (work / "out").mkdir()
    (work / OPENSSL_CONFIG).write_text("")


def _checkout(work: Path, indexer: str, files: list[dict[str, Any]]) -> None:
    src = work / "src"
    for f in files:
        target = src / f["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f["text"].encode("utf-8"))
    _scratch(work)
    (work / "environment.json").write_text("[]")
    if indexer == "scip-typescript":
        (src / "tsconfig.json").write_text(json.dumps(tsconfig([f["path"] for f in files])))
    if indexer == "scip-python":
        roots = python_roots([f["path"] for f in files])
        (src / "pyrightconfig.json").write_text(json.dumps({"extraPaths": roots}))


async def run_one(
    config: Config, indexer: str, files: list[dict[str, Any]], *, allow_unsandboxed: bool
) -> tuple[Outcome, list[dict[str, Any]]]:
    version = await asyncio.to_thread(installed, config, indexer)
    if version is None:
        return Outcome(indexer, len(files), "failed", "not_installed"), []
    work = Path(tempfile.mkdtemp(prefix="codehover-index-", dir=config.scratch))
    try:
        await asyncio.to_thread(_checkout, work, indexer, files)
        paths = sandbox.IndexerPaths(node=config.node, tools=config.tools, work=str(work))
        request = {
            "indexer": indexer,
            "node": config.node,
            "tools": config.tools,
            "work": str(work),
            "timeout": config.timeout,
            "heap_mb": config.heap_mb,
        }
        try:
            result = await sandbox.run(
                "app.engine.index_worker",
                request,
                allow_unsandboxed=allow_unsandboxed,
                indexer=paths,
                wall_seconds=config.timeout + SANDBOX_GRACE_SECONDS,
            )
        except sandbox.SandboxError as error:
            return Outcome(indexer, len(files), "failed", f"sandbox_{error.reason}", version), []
    finally:
        await asyncio.to_thread(shutil.rmtree, work, True)
    if result.get("status") != "ok":
        return Outcome(indexer, len(files), "failed", str(result.get("reason")), version), []
    return Outcome(indexer, len(files), "ok", None, version), list(result.get("documents", []))


async def self_check(config: Config, *, allow_unsandboxed: bool = False) -> dict[str, Any]:
    """The worker's startup probe of the indexer profile (sandbox.self_check), in a scratch directory."""
    if not all(await asyncio.to_thread(lambda: [installed(config, i) for i in PACKAGES])):
        raise sandbox.SandboxError("indexer_not_installed")
    work = Path(tempfile.mkdtemp(prefix="codehover-probe-", dir=config.scratch))
    try:
        _scratch(work)
        paths = sandbox.IndexerPaths(node=config.node, tools=config.tools, work=str(work))
        return await sandbox.self_check(allow_unsandboxed=allow_unsandboxed, indexer=paths)
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def resolve(
    config: Config, files: list[dict[str, Any]], *, allow_unsandboxed: bool = False
) -> tuple[dict[str, dict[str, Any]], list[Outcome]]:
    """Resolved occurrences per file ID (see index.build_index) and one outcome per indexer that ran.
    Symbols are namespaced by indexer, so the two indexes never share an ID."""
    by_path = {f["path"]: f for f in files if _safe(f["path"])}
    resolved: dict[str, dict[str, Any]] = {}
    outcomes = []
    for indexer, languages in FAMILIES.items():
        family = [f for f in by_path.values() if f["language"] in languages]
        if not family:
            continue
        outcome, documents = await run_one(config, indexer, family, allow_unsandboxed=allow_unsandboxed)
        outcomes.append(outcome)
        for document in documents:
            f = by_path.get(document["path"])
            if f is None or f["language"] not in languages:
                continue
            resolved[f["file_id"]] = {
                "definitions": [
                    (line, col, name, f"{indexer}:{sym}") for line, col, name, sym in document["definitions"]
                ],
                "references": [(line, col, f"{indexer}:{sym}") for line, col, sym in document["references"]],
            }
    return resolved, outcomes
