"""Type-aware references from SCIP indexers (T23), started by the worker's index stage.

For each language family with selected files, the worker writes those files (and, for Rust, the
repository's Cargo manifests beside them, nothing else) into a fresh scratch checkout, then runs
app.engine.index_worker in the sandbox's indexer profile. Any failure (timeout, crash, unusable index,
missing tools) leaves that family on name matching, labeled `name` as before (07, A12); one indexer
failing never fails the build.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from app.engine import sandbox, ts_project
from app.engine.index_worker import MANIFESTS, OPENSSL_CONFIG, PACKAGE_FILES, RUST_CONFIG, TSCONFIGS

FAMILIES = {
    "scip-python": frozenset({"python"}),
    "scip-typescript": frozenset({"typescript", "tsx", "javascript"}),
    "rust-analyzer": frozenset({"rust"}),
}
PACKAGES = {"scip-python": "@sourcegraph/scip-python", "scip-typescript": "@sourcegraph/scip-typescript"}
SANDBOX_GRACE_SECONDS = 60  # the sandbox's own wall clock outlasts the indexer timeout inside it
MAX_MANIFESTS = 500
MAX_TSCONFIGS = ts_project.MAX_CONFIGS
MAX_PACKAGES = ts_project.MAX_PACKAGES
# rust-analyzer's own settings: no proc-macro server, no build scripts, no cargo (the crate graph comes
# from rust-project.json), no diagnostics. The repository's rust-analyzer.toml is never in the checkout.
RUST_ANALYZER_SETTINGS = {
    "procMacro": {"enable": False},
    "cargo": {"buildScripts": {"enable": False}, "autoreload": False},
    "checkOnSave": False,
    "diagnostics": {"enable": False},
}


@dataclass(frozen=True)
class Config:
    node: str | None = None
    tools: str | None = None  # the installed npm indexer packages
    timeout: int = 300
    heap_mb: int = 1536
    scratch: str | None = None  # parent of the scratch checkouts; the system temp directory by default
    rust_analyzer: str | None = None  # the pinned rust-analyzer binary
    rust_src: str | None = None  # Rust's standard library sources (rust-src's `library` directory)
    threads: int = 2

    def program(self, indexer: str) -> tuple[str, str] | None:
        """(program, tools) of an indexer, or None when this worker lacks it."""
        rust = indexer == "rust-analyzer"
        pair = (self.rust_analyzer, self.rust_src) if rust else (self.node, self.tools)
        return (pair[0], pair[1]) if pair[0] and pair[1] else None


@dataclass(frozen=True)
class Outcome:
    indexer: str
    files: int
    status: str  # "ok" or "failed"
    reason: str | None = None
    version: str | None = None


def versions(config: Config) -> dict[str, str]:
    """Installed indexer versions, recorded in the build manifest. rust-analyzer's is the release name
    the image recorded beside the binary (`VERSION`), so nothing is executed to learn it."""
    found = {}
    for indexer, package in PACKAGES.items():
        if config.tools is None:
            break
        manifest = Path(config.tools) / "node_modules" / package / "package.json"
        try:
            found[indexer] = str(json.loads(manifest.read_text())["version"])
        except (OSError, ValueError, KeyError):
            continue
    if config.rust_analyzer and config.rust_src:
        try:
            version = (Path(config.rust_analyzer).parent / "VERSION").read_text().strip()
        except OSError:
            version = ""
        if version and Path(config.rust_src, "core", "src", "lib.rs").is_file():
            found["rust-analyzer"] = version
    return found


def installed(config: Config, indexer: str) -> str | None:
    """The indexer's version when it and its program are present, else None."""
    program = config.program(indexer)
    return versions(config).get(indexer) if program and Path(program[0]).is_file() else None


def tsconfig(paths: list[str]) -> dict[str, Any]:
    """The TypeScript configuration of the checkout root: the indexer's own options and the selected
    files. A repository's own tsconfig is never loaded; the sandboxed worker reads only its path aliases
    (app.engine.ts_project) and then writes projects of its own beside the checkout."""
    return {"compilerOptions": dict(ts_project.BASE_OPTIONS), "files": paths}


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


def _checkout(
    work: Path,
    indexer: str,
    files: list[dict[str, Any]],
    manifests: list[dict[str, Any]] | None = None,
    tsconfigs: list[dict[str, Any]] | None = None,
) -> None:
    src = work / "src"
    src.mkdir()
    for f in files:
        target = src / f["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f["text"].encode("utf-8"))
    _scratch(work)
    if indexer == "rust-analyzer":
        for m in (manifests or [])[:MAX_MANIFESTS]:
            if _safe(m["path"]) and PurePosixPath(m["path"]).name == "Cargo.toml":
                target = work / MANIFESTS / m["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(m["text"].encode("utf-8"))
        (work / RUST_CONFIG).write_text(json.dumps(RUST_ANALYZER_SETTINGS))
        # The sandboxed worker writes the crate graph to out/; rust-analyzer finds it above the checkout.
        (work / "rust-project.json").symlink_to(Path("out") / "rust-project.json")
    if indexer == "scip-typescript":
        (src / "tsconfig.json").write_text(json.dumps(tsconfig([f["path"] for f in files])))
        # scip-typescript names a file's package from the nearest package.json, searching upwards until its
        # project's root. The generated projects (in out) are not above the checkout, so the search would
        # leave the scratch directory; this fixed one ends it here for every project alike.
        (work / "package.json").write_text(json.dumps({"name": "codehover", "version": "0.0.0"}))
        # The repository's configs and package manifests as data, beside the checkout: the sandboxed
        # worker reads their module resolution (app.engine.ts_project).
        configs = [c for c in tsconfigs or [] if _safe(c["path"]) and ts_project.is_config(c["path"])]
        packages = [c for c in tsconfigs or [] if _safe(c["path"]) and ts_project.is_package(c["path"])]
        for folder, chosen in (
            (TSCONFIGS, configs[:MAX_TSCONFIGS]),
            (PACKAGE_FILES, packages[:MAX_PACKAGES]),
        ):
            for c in chosen:
                target = work / folder / c["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(c["text"].encode("utf-8"))
    if indexer == "scip-python":
        (work / "environment.json").write_text("[]")
        roots = python_roots([f["path"] for f in files])
        (src / "pyrightconfig.json").write_text(json.dumps({"extraPaths": roots}))


async def run_one(
    config: Config,
    indexer: str,
    files: list[dict[str, Any]],
    *,
    allow_unsandboxed: bool,
    manifests: list[dict[str, Any]] | None = None,
    tsconfigs: list[dict[str, Any]] | None = None,
) -> tuple[Outcome, list[dict[str, Any]]]:
    version = await asyncio.to_thread(installed, config, indexer)
    program = config.program(indexer)
    if version is None or program is None:
        return Outcome(indexer, len(files), "failed", "not_installed"), []
    work = Path(tempfile.mkdtemp(prefix="codehover-index-", dir=config.scratch))
    try:
        await asyncio.to_thread(_checkout, work, indexer, files, manifests, tsconfigs)
        paths = sandbox.IndexerPaths(program=program[0], tools=program[1], work=str(work))
        request = {
            "indexer": indexer,
            "program": program[0],
            "tools": program[1],
            "work": str(work),
            "timeout": config.timeout,
            "heap_mb": config.heap_mb,
            "threads": config.threads,
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


def _probe_arguments(indexer: str, work: Path) -> tuple[str, ...]:
    if indexer == "rust-analyzer":
        return ("--version",)
    return (f"--openssl-config={work / OPENSSL_CONFIG}", "-e", "0")  # Node reads one at startup


async def self_check(config: Config, *, allow_unsandboxed: bool = False) -> dict[str, dict[str, Any]]:
    """The worker's startup probe of the indexer profile (sandbox.self_check), once per configured
    program, each in a scratch directory. A configured indexer that is not installed fails the check."""
    reports: dict[str, dict[str, Any]] = {}
    probed: set[str] = set()
    for indexer in FAMILIES:
        program = config.program(indexer)
        if program is None:
            continue  # not configured on this worker
        if not await asyncio.to_thread(installed, config, indexer):
            raise sandbox.SandboxError("indexer_not_installed")
        if program[0] in probed:
            continue  # the same program (Node for scip-python and scip-typescript) is probed once
        probed.add(program[0])
        work = Path(tempfile.mkdtemp(prefix="codehover-probe-", dir=config.scratch))
        try:
            _scratch(work)
            paths = sandbox.IndexerPaths(
                program=program[0], tools=program[1], work=str(work), probe=_probe_arguments(indexer, work)
            )
            reports[indexer] = await sandbox.self_check(allow_unsandboxed=allow_unsandboxed, indexer=paths)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return reports


async def resolve(
    config: Config,
    files: list[dict[str, Any]],
    *,
    allow_unsandboxed: bool = False,
    manifests: list[dict[str, Any]] | None = None,
    tsconfigs: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[Outcome]]:
    """Resolved occurrences per file ID (see index.build_index) and one outcome per indexer that ran.
    Symbols are namespaced by indexer, so two indexes never share an ID. `manifests` are the snapshot's
    `Cargo.toml` files ({"path", "text"}), read only by the Rust family; `tsconfigs` its `tsconfig*.json`
    and `jsconfig*.json` files, whose path aliases only the TypeScript family reads."""
    by_path = {f["path"]: f for f in files if _safe(f["path"])}
    resolved: dict[str, dict[str, Any]] = {}
    outcomes = []
    for indexer, languages in FAMILIES.items():
        family = [f for f in by_path.values() if f["language"] in languages]
        if not family:
            continue
        outcome, documents = await run_one(
            config,
            indexer,
            family,
            allow_unsandboxed=allow_unsandboxed,
            manifests=manifests,
            tsconfigs=tsconfigs,
        )
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
