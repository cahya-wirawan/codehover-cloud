"""Parser, reference graph, replay explanations and the sandbox (T08; 07, 22 "Sandbox gate")."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from app.engine import sandbox
from app.engine.index import build_index
from app.engine.parse_worker import _languages, parse_file
from app.engine.replay import SCHEMA_PATH, explain_file

LANGS = _languages()
PY = '''"""Module."""


def helper(a, b=2, *rest):
    """Add things. More detail."""
    return a + b


class Store:
    def get(self, key):
        return helper(key)
'''
TS = 'const s = "é€"; function badge(n: number): string { return String(n) }\nexport class Box { open() { return badge(1) } }\n'


def test_python_definitions_methods_docstrings_and_params() -> None:
    parsed = parse_file(LANGS["python"], "python", PY)
    by_key = {s["symbol_key"]: s for s in parsed["symbols"]}
    assert set(by_key) == {"helper", "Store", "Store.get"}
    assert by_key["Store.get"]["kind"] == "method" and by_key["Store.get"]["parent_key"] == "Store"
    assert by_key["helper"]["docstring"].startswith("Add things.")
    assert by_key["helper"]["params"] == ["a", "b", "rest"]
    assert (by_key["helper"]["start_line"], by_key["helper"]["start_column"]) == (4, 0)
    assert parsed["error"] is None


def test_typescript_unicode_columns_are_scalar_offsets() -> None:
    parsed = parse_file(LANGS["typescript"], "typescript", TS)
    badge = next(s for s in parsed["symbols"] if s["name"] == "badge")
    assert TS.split("\n")[0].index("function badge") == badge["start_column"]  # 2 multi-byte chars before it
    assert {s["symbol_key"] for s in parsed["symbols"]} == {"badge", "Box", "Box.open"}


def test_syntax_errors_still_parse_partially() -> None:
    parsed = parse_file(LANGS["python"], "python", "def ok():\n    pass\n\ndef broken(:\n")
    assert parsed["error"] == "syntax_error" and any(s["name"] == "ok" for s in parsed["symbols"])


def test_references_are_name_based_and_ambiguity_is_explicit() -> None:
    a = {"file_id": "a", **parse_file(LANGS["python"], "python", "def run():\n    return go()\n")}
    b = {"file_id": "b", **parse_file(LANGS["python"], "python", "def go():\n    pass\n")}
    c = {"file_id": "c", **parse_file(LANGS["python"], "python", "def go():\n    pass\n")}
    graph = build_index([a, b, c], {"a": "src/main.py", "b": "src/x.py", "c": "tests/test_x.py"})
    edges = [(e.from_key, e.to_key, e.ambiguity_group) for e in graph.edges]
    assert sorted(edges) == [("a::run", "b::go", "go"), ("a::run", "c::go", "go")]
    ranks = {k: s.rank for k, s in graph.symbols.items()}
    assert ranks["b::go"] > ranks["c::go"]  # test files are downweighted


def test_replay_output_satisfies_the_explanation_contract() -> None:
    parsed = parse_file(LANGS["python"], "python", PY)
    graph = build_index([{"file_id": "f", **parsed}], {"f": "app/store.py"})
    explanation = explain_file("app/store.py", list(graph.symbols.values()))
    assert {s["symbol_key"] for s in explanation["symbols"]} == {"helper", "Store", "Store.get"}
    helper = next(s for s in explanation["symbols"] if s["symbol_key"] == "helper")
    assert helper["summary"] == "Add things." and [p["name"] for p in helper["params"]] == ["a", "b", "rest"]


def test_backend_schema_copy_matches_the_spec_contract() -> None:
    spec = Path(__file__).resolve().parents[3] / "build-package" / "contracts" / "explanation.schema.json"
    if not spec.exists():
        pytest.skip("build-package not present")
    assert json.loads(SCHEMA_PATH.read_text()) == json.loads(spec.read_text())


def test_parser_runs_inside_the_sandbox() -> None:
    payload = {"files": [{"file_id": "1", "path": "a.py", "language": "python", "text": PY}]}
    result = asyncio.run(sandbox.run("app.engine.parse_worker", payload, allow_unsandboxed=True))
    assert {s["symbol_key"] for s in result["files"][0]["symbols"]} == {"helper", "Store", "Store.get"}


def test_only_allowlisted_modules_run_in_the_sandbox() -> None:
    with pytest.raises(sandbox.SandboxError, match="exit_2"):
        asyncio.run(sandbox.run("os", {}, allow_unsandboxed=True))


@pytest.mark.skipif(sys.platform != "linux", reason="seccomp network isolation is enforced on Linux")
def test_sandbox_blocks_network_and_inherits_no_environment() -> None:
    """M1 gate (22): the parser process cannot open sockets and sees no credentials."""
    report = asyncio.run(sandbox.self_check())
    assert report["isolated"] is True
    assert (report["inet"], report["inet6"], report["unix"]) == (False, False, False)
    assert report["environment"] == []


@pytest.mark.skipif(sys.platform == "linux", reason="non-Linux behavior")
def test_non_linux_refuses_to_run_without_explicit_development_override() -> None:
    with pytest.raises(sandbox.SandboxError, match="exit_3"):
        asyncio.run(sandbox.run("app.engine.sandbox_probe", {}))
