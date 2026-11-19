"""Run one SCIP indexer, inside the sandbox's indexer profile (app.engine.sandbox_exec, T23).

stdin:  {"indexer": "scip-python" | "scip-typescript", "node": str, "tools": str, "work": str,
         "timeout": int, "heap_mb": int}
stdout: {"status": "ok", "indexer": str, "documents": [{"path": str,
            "definitions": [[line, column, name, symbol]], "references": [[line, column, symbol]]}]}
        or {"status": "failed", "indexer": str, "reason": str}

`<work>/src` holds the build's selected files (plus the tsconfig the worker wrote for TypeScript); the
indexer writes `<work>/out/index.scip`. The OS sandbox is the boundary: no sockets, no program but Node,
reads limited to runtimes, the indexer packages and the checkout, writes to `out`. scip-typescript also
runs under Node's permission model (no child processes, workers, addons or WASI). Nothing is installed
and no repository script, hook or config runs: the checkout holds source files only. Output positions match the parser's (02/06): lines 1-based, columns
in Unicode scalars. Symbols are small integers, local symbols scoped to their document.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.engine import scip

ENTRY_POINTS = {
    "scip-python": "@sourcegraph/scip-python/index.js",
    "scip-typescript": "@sourcegraph/scip-typescript/dist/src/main.js",
}
# Node's permission model, a second layer inside the OS sandbox. scip-python cannot run under it: its
# bundled @yarnpkg/fslib patches the `path` module, which the permission model freezes.
NODE_PERMISSION = frozenset({"scip-typescript"})
WORKER_FILES = frozenset(
    {"tsconfig.json", "pyrightconfig.json"}
)  # written by the worker at the checkout root
OPENSSL_CONFIG = "openssl.cnf"  # empty; written by the worker beside the checkout (Node reads one at startup)
MAX_INDEX_BYTES = 200 * 1024**2
MAX_REFERENCES_PER_FILE = 20000  # as the parser (parse_worker)
# Line breaks the indexers count but tree-sitter does not; such documents keep name matching.
FOREIGN_BREAKS = ("\u2028", "\u2029", "\x85")


def command(request: dict[str, Any]) -> list[str]:
    tools, work = Path(request["tools"]), Path(request["work"])
    src, out = work / "src", work / "out"
    arguments = {
        "scip-python": [
            "index",
            "--quiet",
            "--project-name",
            "codehover",
            "--project-version",
            "0",
            "--cwd",
            str(src),
            "--output",
            str(out / "index.scip"),
            "--environment",
            str(work / "environment.json"),  # an empty package list: scip-python never runs pip
        ],
        "scip-typescript": [
            "index",
            "--no-progress-bar",
            "--cwd",
            str(src),
            "--output",
            str(out / "index.scip"),
        ],
    }[request["indexer"]]
    permission = (
        ["--permission", f"--allow-fs-read={tools}", f"--allow-fs-read={work}", f"--allow-fs-write={out}"]
        if request["indexer"] in NODE_PERMISSION
        else []
    )
    return [
        request["node"],
        f"--max-old-space-size={int(request['heap_mb'])}",
        *permission,
        f"--openssl-config={work / OPENSSL_CONFIG}",  # not /etc/ssl: the sandbox cannot read /etc
        str(tools / "node_modules" / ENTRY_POINTS[request["indexer"]]),
        *arguments,
    ]


def _usable(text: str) -> bool:
    """The indexers count lone CR, LS, PS and NEL as line breaks and skip a leading BOM; tree-sitter does
    neither, so positions in such files would not line up."""
    if text.startswith("\ufeff") or any(mark in text for mark in FOREIGN_BREAKS):
        return False
    return "\r" not in text.replace("\r\n", "")


def compact(documents: list[scip.Document], texts: dict[str, str]) -> list[dict[str, Any]]:
    """Definitions and references of the given files, in the parser's coordinates."""
    symbols: dict[str, int] = {}

    def symbol_id(path: str, symbol: str) -> int:
        key = f"{path}\0{symbol}" if symbol.startswith("local ") else symbol  # locals are per document
        return symbols.setdefault(key, len(symbols))

    result = []
    for document in documents:
        text = texts.get(document.path)
        if text is None or not _usable(text):
            continue
        lines = text.split("\n")
        definitions: list[list[Any]] = []
        references: list[list[Any]] = []
        for o in document.occurrences:
            if o.line >= len(lines) or (o.definition and o.end_line != o.line):
                continue
            line = lines[o.line]
            column = scip.scalar_column(line, o.start, document.encoding)
            if column is None:
                continue
            if o.definition:
                end = scip.scalar_column(line, o.end, document.encoding)
                if end is not None and end > column:
                    definitions.append(
                        [o.line + 1, column, line[column:end], symbol_id(document.path, o.symbol)]
                    )
            elif len(references) < MAX_REFERENCES_PER_FILE:
                references.append([o.line + 1, column, symbol_id(document.path, o.symbol)])
        result.append({"path": document.path, "definitions": definitions, "references": references})
    return result


def _sources(src: Path) -> dict[str, str]:
    texts = {}
    for path in src.rglob("*"):
        if path.is_file() and not (path.parent == src and path.name in WORKER_FILES):
            texts[path.relative_to(src).as_posix()] = path.read_bytes().decode("utf-8")
    return texts


def run(request: dict[str, Any]) -> dict[str, Any]:
    indexer = request["indexer"]
    work = Path(request["work"])
    out = work / "out"

    def failed(reason: str) -> dict[str, Any]:
        return {"status": "failed", "indexer": indexer, "reason": reason}

    with (out / "indexer.log").open("wb") as log:
        process = subprocess.Popen(  # noqa: S603 - fixed argv: the pinned Node binary and indexer
            command(request),
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
            cwd=work / "src",
            env={"HOME": str(work), "LANG": "C.UTF-8"},
        )
        assert process.stdin is not None
        process.stdin.close()
        try:
            code = process.wait(timeout=request["timeout"])
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            return failed("timeout")
    if code != 0:
        return failed("exit")
    target = out / "index.scip"
    if not target.is_file():
        return failed("no_output")
    if target.stat().st_size > MAX_INDEX_BYTES:
        return failed("too_large")
    try:
        documents = scip.documents(target.read_bytes())
    except scip.ScipError:
        return failed("invalid_index")
    return {"status": "ok", "indexer": indexer, "documents": compact(documents, _sources(work / "src"))}


def main() -> int:
    request = json.loads(sys.stdin.read())
    if request.get("indexer") not in ENTRY_POINTS:
        sys.stdout.write(json.dumps({"status": "failed", "indexer": None, "reason": "unknown_indexer"}))
        return 0
    os.umask(0o077)
    sys.stdout.write(json.dumps(run(request)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
