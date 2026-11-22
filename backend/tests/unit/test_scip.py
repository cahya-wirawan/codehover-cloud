"""SCIP indexers (T23): index decoding, position mapping, edges, fallback and the indexer sandbox."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from app.engine import index_worker, indexers, sandbox, scip
from app.engine.index import build_index
from app.engine.parse_worker import _languages, parse_file

LANGS = _languages()


# ---------------------------------------------------------------- a minimal protobuf encoder for fixtures
def _varint(value: int) -> bytes:
    value &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field(number: int, value: int | bytes | str) -> bytes:
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    data = value.encode() if isinstance(value, str) else value
    return _varint(number << 3 | 2) + _varint(len(data)) + data


def occurrence(span: list[int], symbol: str, roles: int = 0, *, packed: bool = True) -> bytes:
    ranges = _field(1, b"".join(_varint(v) for v in span)) if packed else b"".join(_field(1, v) for v in span)
    return ranges + _field(2, symbol) + (_field(3, roles) if roles else b"")


def document(path: str, occurrences: list[bytes], encoding: int = 0) -> bytes:
    body = _field(1, path) + _field(4, "Python") + b"".join(_field(2, o) for o in occurrences)
    return body + (_field(6, encoding) if encoding else b"")


def index(*documents: bytes) -> bytes:
    metadata = _field(2, _field(1, "scip-test") + _field(2, "1.0"))
    return _field(1, metadata) + b"".join(_field(2, d) for d in documents) + _field(3, _field(1, "ext#"))


# ---------------------------------------------------------------- decoding
def test_documents_decode_ranges_roles_and_encodings() -> None:
    data = index(
        document(
            "pkg/a.py",
            [
                occurrence([0, 4, 10], "s helper().", roles=scip.DEFINITION),
                occurrence([3, 2, 4, 1], "s helper().", roles=8),  # four-element range
                occurrence([5, 1, 2], "local 0", packed=False),  # unpacked repeated int32 is valid too
            ],
        ),
        document("web/b.ts", [occurrence([1, 0, 3], "s b().")], encoding=scip.UTF8),
    )
    first, second = scip.documents(data)
    assert first.path == "pkg/a.py" and first.encoding == scip.UTF16  # unspecified means UTF-16
    assert [(o.line, o.start, o.end_line, o.end, o.symbol, o.definition) for o in first.occurrences] == [
        (0, 4, 0, 10, "s helper().", True),
        (3, 2, 4, 1, "s helper().", False),
        (5, 1, 5, 2, "local 0", False),
    ]
    assert second.encoding == scip.UTF8


def test_malformed_occurrences_are_dropped_but_malformed_indexes_refused() -> None:
    data = index(document("a.py", [occurrence([0, 1], "s x."), occurrence([0, 1, 2], "")]))
    assert scip.documents(data)[0].occurrences == []  # bad range, anonymous symbol
    good = index(document("a.py", [occurrence([0, 1, 2], "s x.")]))
    for broken in (good[:-3], good + b"\x0b", b"\x12\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01"):
        with pytest.raises(scip.ScipError):
            scip.documents(broken)
    many = index(document("a.py", [occurrence([0, 1, 2], "s x.")] * 3))
    with pytest.raises(scip.ScipError, match="too many"):
        scip.documents(many, max_occurrences=2)


def test_columns_convert_to_unicode_scalars() -> None:
    line = "é😀x = 1"  # é: 2 UTF-8 bytes, 1 UTF-16 unit; 😀: 4 bytes, 2 units
    assert scip.scalar_column(line, 3, scip.UTF16) == 2  # x
    assert scip.scalar_column(line, 6, scip.UTF8) == 2
    assert scip.scalar_column(line, 2, scip.UTF32) == 2
    assert scip.scalar_column(line, 2, scip.UTF16) is None  # inside the surrogate pair
    assert scip.scalar_column(line, len(line) + 1, scip.UTF16) == len(
        line
    )  # line end (UTF-16 counts 😀 twice)
    assert scip.scalar_column(line, 99, scip.UTF16) is None


# ---------------------------------------------------------------- the sandboxed worker's conversion
def test_compact_maps_names_lines_and_scopes_locals_per_document() -> None:
    docs = [
        scip.Document(
            "a.py",
            scip.UTF16,
            [
                scip.Occurrence(0, 4, 0, 10, "s helper().", scip.DEFINITION),
                scip.Occurrence(1, 4, 1, 5, "local 0", scip.DEFINITION),
                scip.Occurrence(2, 11, 2, 17, "s helper().", 8),
                scip.Occurrence(2, 18, 2, 19, "local 0", 8),
            ],
        ),
        scip.Document("b.py", scip.UTF16, [scip.Occurrence(0, 0, 0, 1, "local 0", 8)]),
        scip.Document("crlf.py", scip.UTF16, [scip.Occurrence(0, 0, 0, 1, "s x.", 8)]),
        scip.Document("lonely-cr.py", scip.UTF16, [scip.Occurrence(0, 0, 0, 1, "s x.", 8)]),
    ]
    texts = {
        "a.py": "def helper(x):\n    y = x\n    return helper(y)\n",
        "b.py": "y\n",
        "crlf.py": "x\r\n",
        "lonely-cr.py": "x\ry\n",  # the indexers count a lone CR as a line break, tree-sitter does not
    }
    a, b, crlf = index_worker.compact(docs, texts)
    helper, local = a["definitions"][0][3], a["definitions"][1][3]
    assert a["definitions"] == [[1, 4, "helper", helper], [2, 4, "y", local]]
    assert a["references"] == [[3, 11, helper], [3, 18, local]]
    assert b["references"][0][2] not in (helper, local)  # "local 0" of another document is another symbol
    assert crlf["path"] == "crlf.py"


def test_node_command_line() -> None:
    request = {"program": "/n/node", "tools": "/t", "work": "/w", "heap_mb": 1024}
    python = index_worker.command({**request, "indexer": "scip-python"})
    assert python[:2] == ["/n/node", "--max-old-space-size=1024"]
    assert "--permission" not in python  # incompatible with scip-python (see NODE_PERMISSION)
    assert python[python.index("--environment") + 1] == "/w/environment.json"  # never pip
    assert "--openssl-config=/w/openssl.cnf" in python
    typescript = index_worker.command({**request, "indexer": "scip-typescript"})
    assert typescript[2:6] == [
        "--permission",
        "--allow-fs-read=/t",
        "--allow-fs-read=/w",
        "--allow-fs-write=/w/out",
    ]
    assert typescript[-4:] == ["--cwd", "/w/src", "--output", "/w/out/index.scip"]


def test_rust_analyzer_command_line() -> None:
    request = {"indexer": "rust-analyzer", "program": "/r/rust-analyzer", "tools": "/lib", "work": "/w"}
    assert index_worker.command({**request, "threads": 3}) == [
        "/r/rust-analyzer",
        "scip",
        "/w/src",  # rust-analyzer finds /w/rust-project.json above the checkout, never a Cargo.toml
        "--output",
        "/w/out/index.scip",
        "--config-path",
        "/w/rust-analyzer.json",
        "--exclude-vendored-libraries",
        "--num-threads",
        "3",
    ]


def test_rust_checkout_holds_sources_manifests_and_the_worker_settings_only() -> None:
    work = Path(tempfile.mkdtemp())
    files = [{"path": "src/lib.rs", "text": "pub fn f() {}\n"}]
    manifests = [
        {"path": "Cargo.toml", "text": '[package]\nname = "x"\n'},
        {"path": "../evil/Cargo.toml", "text": ""},  # never written outside the scratch directory
        {"path": "build.rs", "text": ""},  # only manifests go to manifests/
    ]
    indexers._checkout(work, "rust-analyzer", files, manifests)
    written = sorted(p.relative_to(work).as_posix() for p in work.rglob("*") if p.is_file() or p.is_symlink())
    assert written == [
        "manifests/Cargo.toml",
        "openssl.cnf",
        "rust-analyzer.json",
        "rust-project.json",
        "src/src/lib.rs",
    ]
    assert (work / "rust-project.json").readlink() == Path("out/rust-project.json")
    settings = json.loads((work / "rust-analyzer.json").read_text())
    assert settings["procMacro"] == {"enable": False} and settings["cargo"]["buildScripts"] == {
        "enable": False
    }


def test_worker_configuration_and_paths() -> None:
    roots = indexers.python_roots(
        [
            "backend/app/__init__.py",
            "backend/app/x/__init__.py",
            "backend/app/x/y.py",
            "tools/run.py",
            "top.py",
        ]
    )
    assert roots == ["backend", "tools"]  # the checkout root is always searched
    config = indexers.tsconfig(["web/a.ts", "web/b.tsx"])
    assert config["files"] == ["web/a.ts", "web/b.tsx"] and "extends" not in config
    assert config["compilerOptions"]["allowJs"] and config["compilerOptions"]["types"] == []
    assert [indexers._safe(p) for p in ("a/b.py", "/etc/x.py", "a/../b.py", "a\\b.py", "")] == [
        True,
        False,
        False,
        False,
        False,
    ]


# ---------------------------------------------------------------- edges
PY_UTIL = (
    "def helper(x):\n    return x + 1\n\nclass Greeter:\n    def greet(self, name):\n        return name\n"
)
PY_MAIN = "from util import helper, Greeter\n\ndef run():\n    g = Greeter()\n    return g.greet(helper(1))\n\ndef greet():\n    return run()\n"


def _parsed() -> list[dict[str, Any]]:
    return [
        {"file_id": "u", **parse_file(LANGS["python"], "python", PY_UTIL)},
        {"file_id": "m", **parse_file(LANGS["python"], "python", PY_MAIN)},
    ]


RESOLVED = {
    "u": {
        "definitions": [
            (1, 4, "helper", "p:helper"),
            (4, 6, "Greeter", "p:Greeter"),
            (5, 8, "greet", "p:greet"),
        ],
        "references": [],
    },
    "m": {
        "definitions": [(3, 4, "run", "p:run"), (7, 4, "greet", "p:main.greet")],
        "references": [
            (1, 17, "p:helper"),
            (4, 8, "p:Greeter"),
            (5, 13, "p:greet"),
            (5, 19, "p:helper"),
            (8, 11, "p:run"),
        ],
    },
}


def test_index_edges_follow_the_indexer_not_the_name() -> None:
    paths = {"u": "util.py", "m": "main.py"}
    by_name = build_index(_parsed(), paths)
    assert ("m::run", "m::greet") in {(e.from_key, e.to_key) for e in by_name.edges}  # the false candidate
    graph = build_index(_parsed(), paths, RESOLVED)
    edges = sorted((e.from_key or "", e.to_key, e.resolution, e.ambiguity_group) for e in graph.edges)
    assert edges == [
        ("", "u::helper", "index", None),  # the import
        ("m::greet", "m::run", "index", None),
        ("m::run", "u::Greeter", "index", None),
        ("m::run", "u::Greeter.greet", "index", None),  # g.greet is the method, not main.greet
        ("m::run", "u::helper", "index", None),
    ]
    assert graph.symbols["u::helper"].in_degree == 2 and graph.symbols["m::greet"].in_degree == 0


def test_unresolved_files_keep_labeled_name_matching() -> None:
    graph = build_index(_parsed(), {"u": "util.py", "m": "main.py"}, {"u": RESOLVED["u"]})
    assert {e.resolution for e in graph.edges} == {"name"}
    assert any(e.ambiguity_group == "greet" for e in graph.edges)  # A12: candidates stay visible


def test_a_redefinition_is_an_ambiguous_index_edge() -> None:
    text = "def f():\n    return 1\n\ndef f():\n    return 2\n\ndef g():\n    return f()\n"
    parsed = [{"file_id": "a", **parse_file(LANGS["python"], "python", text)}]
    resolved = {"a": {"definitions": [(1, 4, "f", "s"), (4, 4, "f", "s")], "references": [(8, 11, "s")]}}
    edges = build_index(parsed, {"a": "a.py"}, resolved).edges
    assert sorted((e.to_key, e.resolution, e.ambiguity_group) for e in edges) == [
        ("a::f", "index", "f"),
        ("a::f#2", "index", "f"),
    ]


def test_resolve_namespaces_indexers_and_falls_back_per_family(monkeypatch: pytest.MonkeyPatch) -> None:
    files = [
        {"file_id": "1", "path": "a.py", "language": "python", "text": ""},
        {"file_id": "2", "path": "b.ts", "language": "typescript", "text": ""},
        {"file_id": "3", "path": "../c.py", "language": "python", "text": ""},  # never written anywhere
    ]
    seen: dict[str, list[str]] = {}

    async def fake(config: Any, indexer: str, family: list[dict[str, Any]], **_: Any) -> Any:
        seen[indexer] = [f["path"] for f in family]
        if indexer == "scip-typescript":
            return indexers.Outcome(indexer, len(family), "failed", "timeout"), []
        document = {"path": "a.py", "definitions": [[1, 4, "f", 0]], "references": [[2, 0, 0]]}
        return indexers.Outcome(indexer, len(family), "ok"), [document, {**document, "path": "b.ts"}]

    monkeypatch.setattr(indexers, "run_one", fake)
    resolved, outcomes = asyncio.run(indexers.resolve(indexers.Config("/n", "/t"), files))
    assert seen == {"scip-python": ["a.py"], "scip-typescript": ["b.ts"]}
    assert resolved == {
        "1": {"definitions": [(1, 4, "f", "scip-python:0")], "references": [(2, 0, "scip-python:0")]}
    }
    assert [(o.indexer, o.status, o.reason) for o in outcomes] == [
        ("scip-python", "ok", None),
        ("scip-typescript", "failed", "timeout"),
    ]


def test_missing_tools_are_a_labeled_fallback_not_a_failure() -> None:
    config = indexers.Config(node="/nonexistent/node", tools="/nonexistent")
    outcome, documents = asyncio.run(
        indexers.run_one(config, "scip-python", [{"path": "a.py", "text": ""}], allow_unsandboxed=True)
    )
    assert (outcome.status, outcome.reason, documents) == ("failed", "not_installed", [])


def test_profiles_only_run_their_own_modules() -> None:
    with pytest.raises(sandbox.SandboxError, match="exit_2"):
        asyncio.run(sandbox.run("app.engine.index_worker", {}, allow_unsandboxed=True))  # parser profile
    paths = sandbox.IndexerPaths(program="/n", tools="/t", work="/w")
    with pytest.raises(sandbox.SandboxError, match="exit_2"):
        asyncio.run(sandbox.run("app.engine.parse_worker", {}, allow_unsandboxed=True, indexer=paths))


# ---------------------------------------------------------------- the real indexers (Linux, tools installed)
NODE = os.environ.get("SCIP_NODE_PATH", "")
TOOLS = os.environ.get("SCIP_TOOLS_DIR", "")
HAVE_TOOLS = sys.platform == "linux" and Path(NODE).is_file() and Path(TOOLS, "node_modules").is_dir()
if os.environ.get("CODEHOVER_REQUIRE_SCIP") == "1" and not HAVE_TOOLS:
    raise RuntimeError("CODEHOVER_REQUIRE_SCIP=1 but SCIP_NODE_PATH/SCIP_TOOLS_DIR are not usable on Linux")
needs_tools = pytest.mark.skipif(
    not HAVE_TOOLS, reason="needs Linux and the SCIP indexers (SCIP_NODE_PATH, SCIP_TOOLS_DIR)"
)

FIXTURE = [
    ("pkg/__init__.py", "python", ""),
    ("pkg/util.py", "python", PY_UTIL),
    ("pkg/main.py", "python", PY_MAIN.replace("from util", "from pkg.util")),
    (
        "web/a.ts",
        "typescript",
        'export function helper(n: number): number { return n + 1; }\nexport class Box { open(): string { return "é"; } }\n',
    ),
    (
        "web/b.tsx",
        "tsx",
        'import { helper, Box } from "./a";\nexport function main(): string { return new Box().open() + helper(2); }\nfunction open(): number { return 3; }\n',
    ),
]


def _fixture() -> list[dict[str, Any]]:
    return [
        {"file_id": path, "path": path, "language": language, "text": text}
        for path, language, text in FIXTURE
    ]


@needs_tools
def test_indexer_sandbox_runs_node_and_nothing_else() -> None:
    """T23 gate (22): no network, no other program, no credentials, writes only to the scratch `out`."""
    reports = asyncio.run(indexers.self_check(indexers.Config(NODE, TOOLS)))
    assert list(reports) == ["scip-python"]  # one probe per program: both npm indexers run on Node
    report = reports["scip-python"]
    assert report["profile"] == "indexer" and report["isolated"] is True
    assert (report["inet"], report["inet6"], report["unix"]) == (False, False, False)
    assert report["environment"] == []
    closed = ("parent_environ", "etc", "secrets", "exec", "write_code", "write_tmp", "write_work")
    assert [report[k] for k in closed] == [False] * len(closed)  # exec: neither Python nor /bin/sh
    assert report["exec_program"] is True and report["write_out"] is True


@needs_tools
def test_real_indexers_resolve_methods_and_imports() -> None:
    files = _fixture()
    resolved, outcomes = asyncio.run(indexers.resolve(indexers.Config(NODE, TOOLS), files))
    assert [(o.indexer, o.status) for o in outcomes] == [("scip-python", "ok"), ("scip-typescript", "ok")]
    assert set(resolved) == {path for path, _, _ in FIXTURE}
    parsed = [
        {"file_id": f["path"], **parse_file(LANGS[f["language"]], f["language"], f["text"])} for f in files
    ]
    graph = build_index(parsed, {f["path"]: f["path"] for f in files}, resolved)
    edges = {(e.from_key, e.to_key) for e in graph.edges}
    assert {e.resolution for e in graph.edges} == {"index"}
    assert ("pkg/main.py::run", "pkg/util.py::Greeter.greet") in edges
    assert ("pkg/main.py::run", "pkg/main.py::greet") not in edges
    assert ("web/b.tsx::main", "web/a.ts::Box.open") in edges and (
        "web/b.tsx::main",
        "web/b.tsx::open",
    ) not in edges
    assert not any(
        a and a.startswith("pkg/") and b.startswith("web/") for a, b in edges
    )  # no cross-language links


@needs_tools
def test_an_indexer_timeout_falls_back_and_cleans_up() -> None:
    scratch = tempfile.mkdtemp()
    config = indexers.Config(NODE, TOOLS, timeout=0, scratch=scratch)
    resolved, outcomes = asyncio.run(indexers.resolve(config, _fixture()))
    assert resolved == {} and {(o.status, o.reason) for o in outcomes} == {("failed", "timeout")}
    assert list(Path(scratch).iterdir()) == []  # scratch checkouts never outlive the run


ALIASED = [
    ("apps/web/src/util.ts", "typescript", "export function helper(): number { return 1; }\n"),
    (
        "apps/web/src/main.ts",
        "typescript",
        'import { helper } from "@/util";\nexport function run(): number { return helper(); }\n',
    ),
    ("apps/admin/app/util.ts", "typescript", "export function helper(): number { return 2; }\n"),
    (
        "apps/admin/app/main.ts",
        "typescript",
        'import { helper } from "@/util";\nimport { shared } from "@shared/tools";\n'
        "export function run(): number { return helper() + shared(); }\n",
    ),
    ("packages/shared/tools.ts", "typescript", "export function shared(): number { return 3; }\n"),
]
TSCONFIGS = [
    {
        "path": "tsconfig.base.json",
        "text": '{"compilerOptions": {"baseUrl": ".", "paths": {"@shared/*": ["packages/shared/*"]}}}',
    },
    {
        "path": "apps/web/tsconfig.json",
        "text": '{\n  // the app\'s own alias\n  "compilerOptions": {"paths": {"@/*": ["./src/*"],},},\n}',
    },
    {
        "path": "apps/admin/tsconfig.json",
        # The inherited baseUrl (the repository root) is what these targets are relative to.
        "text": '{"extends": "../../tsconfig.base.json", "compilerOptions": {"paths": {"@/*": ["apps/admin/app/*"], "@shared/*": ["packages/shared/*"]}}}',
    },
]


@needs_tools
def test_typescript_path_aliases_resolve_per_project() -> None:
    """Each app's `@/*` resolves to its own folder (07): imports through aliases become `index` edges."""
    files = [{"file_id": p, "path": p, "language": lang, "text": t} for p, lang, t in ALIASED]
    resolved, outcomes = asyncio.run(
        indexers.resolve(indexers.Config(NODE, TOOLS), files, tsconfigs=TSCONFIGS)
    )
    assert [(o.indexer, o.status) for o in outcomes] == [("scip-typescript", "ok")]
    assert set(resolved) == {p for p, _, _ in ALIASED}  # every file indexed once
    parsed = [
        {"file_id": f["path"], **parse_file(LANGS[f["language"]], f["language"], f["text"])} for f in files
    ]
    graph = build_index(parsed, {f["path"]: f["path"] for f in files}, resolved)
    edges = {(e.from_key, e.to_key) for e in graph.edges if e.resolution == "index"}
    assert ("apps/web/src/main.ts::run", "apps/web/src/util.ts::helper") in edges
    assert ("apps/admin/app/main.ts::run", "apps/admin/app/util.ts::helper") in edges
    assert ("apps/admin/app/main.ts::run", "packages/shared/tools.ts::shared") in edges
    assert ("apps/web/src/main.ts::run", "apps/admin/app/util.ts::helper") not in edges
    without, _ = asyncio.run(indexers.resolve(indexers.Config(NODE, TOOLS), files))
    plain = build_index(parsed, {f["path"]: f["path"] for f in files}, without)
    assert ("apps/web/src/main.ts::run", "apps/web/src/util.ts::helper") not in {
        (e.from_key, e.to_key) for e in plain.edges if e.resolution == "index"
    }  # before: aliases were unknown


def test_typescript_projects_are_passed_before_the_options() -> None:
    request = {"indexer": "scip-typescript", "program": "/n", "tools": "/t", "work": "/w", "heap_mb": 512}
    plain = index_worker.command(request)
    assert plain[plain.index("index") + 1] == "--no-progress-bar"  # the checkout's own tsconfig.json
    aliased = index_worker.command(request, ["/w/out/tsconfig.0.json", "/w/out/tsconfig.1.json"])
    start = aliased.index("index") + 1
    assert aliased[start : start + 3] == [
        "/w/out/tsconfig.0.json",
        "/w/out/tsconfig.1.json",
        "--no-progress-bar",
    ]
