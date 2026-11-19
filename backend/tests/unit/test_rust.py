"""Rust (T23): the tree-sitter definitions, the crate graph for rust-analyzer, and the real indexer."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from app.engine import indexers, rust_project
from app.engine.acquire import Entry
from app.engine.index import build_index
from app.engine.parse_worker import _languages, parse_file
from app.engine.scan import scan

RUST = _languages()["rust"]

SAMPLE = """//! The crate.
use crate::shape::Area;

/// Sum of the areas.
/// Second line.
#[inline]
pub fn total(shapes: &[Circle], mut scale: f64, (a, b): (i32, i32)) -> f64 { helper(scale) }

#[derive(Debug)]
pub struct Circle { pub r: f64 }
enum Kind { A, B(i32) }
union Bits { a: u32 }
/** A shape
 * with an area. */
pub trait Area { fn area(&self) -> f64; fn twice(&self) -> f64 { self.area() * 2.0 } }
impl Area for Circle { fn area(&self) -> f64 { self.r } }
impl<T> Wrapper<T> { pub const fn new() -> Self { todo!() } }
impl Circle { fn new() {} }
impl Circle { fn new() {} }
mod nested { pub fn inner() {} }
macro_rules! square { ($x:expr) => { $x * $x }; }
type Meters = f64;
const LIMIT: u32 = 10;
static NAME: &str = "x";
struct Point(i32, i32);
"""


def _symbols() -> dict[str, dict[str, Any]]:
    parsed = parse_file(RUST, "rust", SAMPLE)
    assert parsed["error"] is None
    return {s["symbol_key"]: s for s in parsed["symbols"]}


def test_rust_definitions_map_onto_the_product_kinds() -> None:
    symbols = _symbols()
    assert {k: (s["kind"], s["parent_key"]) for k, s in symbols.items()} == {
        "total": ("function", None),
        "Circle": ("type", None),
        "Kind": ("type", None),
        "Bits": ("type", None),
        "Area": ("interface", None),
        "Area.area": ("method", "Area"),  # a required method has no body
        "Area.twice": ("method", "Area"),
        "Circle.area": ("method", "Circle"),  # an impl block is not a symbol; its items are the type's
        "Wrapper.new": ("method", "Wrapper"),  # generics and the type's own file do not matter
        "Circle.new": ("method", "Circle"),
        "Circle.new#2": ("method", "Circle"),  # a second impl block of the same name stays distinct
        "nested": ("module", None),
        "nested.inner": ("function", "nested"),
        "square": ("function", None),
        "Meters": ("type", None),
        "LIMIT": ("variable", None),
        "NAME": ("variable", None),
        "Point": ("type", None),
    }


def test_rust_signatures_docs_and_parameters() -> None:
    symbols = _symbols()
    total = symbols["total"]
    assert total["signature"] == "pub fn total(shapes: &[Circle], mut scale: f64, (a, b): (i32, i32)) -> f64"
    assert total["params"] == ["shapes", "scale"]  # `self` and destructuring patterns are not names
    assert total["docstring"] == "Sum of the areas.\nSecond line."  # through the #[inline] attribute
    assert total["start_line"] == 7  # the item itself, not its docs or attributes
    assert symbols["Area"]["docstring"] == "A shape\nwith an area."
    assert symbols["Circle"]["signature"] == "pub struct Circle" and symbols["Circle"]["docstring"] is None
    assert symbols["Area.area"]["signature"] == "fn area(&self) -> f64"
    assert symbols["square"]["signature"] == "macro_rules! square"  # the rules are not the signature
    assert symbols["LIMIT"]["signature"] == "const LIMIT: u32 = 10"


def test_rust_references_include_method_calls_and_fields() -> None:
    parsed = parse_file(RUST, "rust", "fn run(c: Circle) -> f64 { c.area() + c.r + helper() }\n")
    names = [r["name"] for r in parsed["references"]]
    assert {"Circle", "area", "r", "helper"} <= set(names) and "run" not in names


def test_rust_files_are_selected_and_syntax_errors_still_parse() -> None:
    files = scan([Entry("src/lib.rs", 10, b"pub fn f() {}\n"), Entry("Cargo.toml", 10, b"[package]\n")])
    assert [(f.path, f.status, f.language) for f in files] == [
        ("Cargo.toml", "unsupported", None),  # a manifest is read by the indexer, never explained
        ("src/lib.rs", "selected", "rust"),
    ]
    parsed = parse_file(RUST, "rust", "pub fn ok() {}\nfn broken( {\n")
    assert parsed["error"] == "syntax_error" and parsed["symbols"][0]["name"] == "ok"


# ---------------------------------------------------------------- the crate graph
WORKSPACE = {
    "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n[workspace.package]\nedition = "2021"\n',
    "crates/geo-core/Cargo.toml": (
        '[package]\nname = "geo-core"\nedition.workspace = true\n[features]\nfast = []\n'
        '[build-dependencies]\nhelper = { path = "../helper" }\n'
    ),
    "crates/geo-cli/Cargo.toml": (
        '[package]\nname = "geo-cli"\nedition = "2024"\n'
        '[dependencies]\ngeo = { package = "geo-core", path = "../geo-core" }\nserde = "1"\n'
        '[dev-dependencies]\nhelper = { path = "../helper" }\n'
        '[[bin]]\nname = "extra"\npath = "tools/extra.rs"\n'
    ),
    "crates/helper/Cargo.toml": '[package]\nname = "helper"\n[lib]\nname = "help"\npath = "lib.rs"\n',
    "crates/broken/Cargo.toml": "[package\nname = ",
}
SOURCES = [
    "crates/geo-core/src/lib.rs",
    "crates/geo-core/src/shape.rs",
    "crates/geo-core/build.rs",
    "crates/geo-core/tests/it.rs",
    "crates/geo-cli/src/main.rs",
    "crates/geo-cli/src/bin/other/main.rs",
    "crates/geo-cli/tools/extra.rs",
    "crates/geo-cli/examples/demo.rs",
    "crates/helper/lib.rs",
    "crates/broken/src/lib.rs",
]


def _graph() -> dict[str, dict[str, Any]]:
    project = rust_project.project(WORKSPACE, SOURCES, "/w/src", "/lib")
    assert project["sysroot_src"] == "/lib"
    crates = project["crates"]
    by_root = {c["root_module"].removeprefix("/w/src/"): c for c in crates}
    for crate in crates:
        crate["dep_names"] = {
            d["name"]: crates[d["crate"]]["root_module"].removeprefix("/w/src/") for d in crate["deps"]
        }
    return by_root


def test_crate_graph_follows_cargo_targets_and_workspace_dependencies() -> None:
    graph = _graph()
    assert set(graph) == {  # broken TOML and the virtual workspace manifest add no crates
        "crates/geo-core/src/lib.rs",
        "crates/geo-core/build.rs",
        "crates/geo-core/tests/it.rs",
        "crates/geo-cli/src/main.rs",
        "crates/geo-cli/src/bin/other/main.rs",
        "crates/geo-cli/tools/extra.rs",
        "crates/geo-cli/examples/demo.rs",
        "crates/helper/lib.rs",
    }
    core = graph["crates/geo-core/src/lib.rs"]
    assert (core["display_name"], core["edition"], core["dep_names"]) == ("geo_core", "2021", {})
    assert 'feature="fast"' in core["cfg"] and "test" in core["cfg"] and "unix" in core["cfg"]
    assert graph["crates/geo-core/build.rs"]["dep_names"] == {"helper": "crates/helper/lib.rs"}
    assert graph["crates/geo-core/tests/it.rs"]["dep_names"] == {"geo_core": "crates/geo-core/src/lib.rs"}
    cli = graph["crates/geo-cli/src/main.rs"]
    assert (cli["display_name"], cli["edition"]) == ("geo_cli", "2024")
    # The renamed path dependency is `geo`; serde (crates.io) is absent; dev-dependencies reach binaries.
    assert cli["dep_names"] == {"geo": "crates/geo-core/src/lib.rs", "helper": "crates/helper/lib.rs"}
    assert graph["crates/geo-cli/tools/extra.rs"]["display_name"] == "extra"
    assert graph["crates/geo-cli/src/bin/other/main.rs"]["display_name"] == "other"
    assert graph["crates/helper/lib.rs"]["display_name"] == "help"  # [lib] name and path
    assert graph["crates/helper/lib.rs"]["edition"] == "2015"  # Cargo's default
    assert all(c["is_workspace_member"] and not c["is_proc_macro"] for c in graph.values())


def test_crate_roots_come_only_from_the_checkout() -> None:
    manifests = {
        "Cargo.toml": '[package]\nname = "x"\n[lib]\npath = "../../etc/passwd"\n[[bin]]\nname = "y"\npath = "/abs.rs"\n'
    }
    assert rust_project.project(manifests, ["src/main.rs"], "/w/src", None)["crates"] == [
        {
            "display_name": "x",
            "root_module": "/w/src/src/main.rs",
            "edition": "2015",
            "deps": [],
            "cfg": list(rust_project.TARGET_CFG),
            "is_workspace_member": True,
            "is_proc_macro": False,
        }
    ]
    lone = rust_project.project({}, ["tool/main.rs", "tool/util.rs", "lib/lib.rs"], "/w/src", None)
    assert [c["root_module"] for c in lone["crates"]] == ["/w/src/lib/lib.rs", "/w/src/tool/main.rs"]
    assert "sysroot_src" not in lone


# ---------------------------------------------------------------- the real indexer (Linux, tools installed)
ANALYZER = os.environ.get("SCIP_RUST_ANALYZER_PATH", "")
LIBRARY = os.environ.get("SCIP_RUST_SRC_DIR", "")
HAVE_RUST = sys.platform == "linux" and Path(ANALYZER).is_file() and Path(LIBRARY, "core").is_dir()
if os.environ.get("CODEHOVER_REQUIRE_SCIP") == "1" and not HAVE_RUST:
    raise RuntimeError(
        "CODEHOVER_REQUIRE_SCIP=1 but SCIP_RUST_ANALYZER_PATH/SCIP_RUST_SRC_DIR are not usable"
    )
needs_rust = pytest.mark.skipif(
    not HAVE_RUST, reason="needs Linux and rust-analyzer (SCIP_RUST_ANALYZER_PATH, SCIP_RUST_SRC_DIR)"
)
CONFIG = indexers.Config(rust_analyzer=ANALYZER, rust_src=LIBRARY)

REPOSITORY = {
    "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n[workspace.package]\nedition = "2021"\n',
    "crates/geo-core/Cargo.toml": '[package]\nname = "geo-core"\nedition.workspace = true\n',
    "crates/geo-core/src/lib.rs": (
        "pub mod shape;\npub use shape::Circle;\nuse shape::Area;\n\n"
        "pub fn total(shapes: &[Circle]) -> f64 {\n    shapes.iter().map(|s| s.area()).sum()\n}\n"
    ),
    "crates/geo-core/src/shape.rs": (
        "pub trait Area { fn area(&self) -> f64; }\n\n"
        "pub struct Circle { pub r: f64 }\n\n"
        "impl Circle {\n    pub fn new(r: f64) -> Self { Circle { r } }\n}\n\n"
        "impl Area for Circle {\n    fn area(&self) -> f64 { self.r * self.r }\n}\n\n"
        "pub struct Square { pub s: f64 }\nimpl Square { pub fn area(&self) -> f64 { self.s } }\n\n"
        "#[cfg(test)]\nmod tests {\n    use super::*;\n    fn unit() -> f64 { Circle::new(1.0).area() }\n}\n"
    ),
    "crates/geo-cli/Cargo.toml": (
        '[package]\nname = "geo-cli"\nedition = "2021"\n'
        '[dependencies]\ngeo = { package = "geo-core", path = "../geo-core" }\nserde = "1"\n'
    ),
    "crates/geo-cli/src/main.rs": (
        "use geo::shape::Area;\nuse geo::Circle;\n\n"
        "fn main() {\n    let shapes: Vec<Circle> = vec![Circle::new(1.0)];\n"
        '    let first = shapes.first().map(|c| c.area());\n    println!("{first:?} {}", geo::total(&shapes));\n}\n'
    ),
}


def _files() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    files = [
        {"file_id": path, "path": path, "language": "rust", "text": text}
        for path, text in REPOSITORY.items()
        if path.endswith(".rs")
    ]
    return files, [{"path": p, "text": t} for p, t in REPOSITORY.items() if p.endswith("Cargo.toml")]


@needs_rust
def test_rust_analyzer_runs_in_the_indexer_sandbox_and_nothing_else() -> None:
    report = asyncio.run(indexers.self_check(CONFIG))["rust-analyzer"]
    assert report["profile"] == "indexer" and report["isolated"] is True
    assert (report["inet"], report["inet6"], report["unix"], report["environment"]) == (
        False,
        False,
        False,
        [],
    )
    closed = ("parent_environ", "etc", "secrets", "exec", "write_code", "write_tmp", "write_work")
    assert [report[k] for k in closed] == [False] * len(closed)
    assert report["exec_program"] is True and report["write_out"] is True
    assert indexers.versions(CONFIG) == {
        "rust-analyzer": (Path(ANALYZER).parent / "VERSION").read_text().strip()
    }


@needs_rust
def test_rust_analyzer_resolves_crates_traits_and_std_types() -> None:
    files, manifests = _files()
    resolved, outcomes = asyncio.run(indexers.resolve(CONFIG, files, manifests=manifests))
    assert [(o.indexer, o.status) for o in outcomes] == [("rust-analyzer", "ok")]
    assert set(resolved) == {f["path"] for f in files}
    parsed = [{"file_id": f["path"], **parse_file(RUST, "rust", f["text"])} for f in files]
    graph = build_index(parsed, {f["path"]: f["path"] for f in files}, resolved)
    edges = {(e.from_key, e.to_key) for e in graph.edges}
    assert {e.resolution for e in graph.edges} == {"index"}
    shape, cli, lib = (
        "crates/geo-core/src/shape.rs",
        "crates/geo-cli/src/main.rs",
        "crates/geo-core/src/lib.rs",
    )
    # Across crates through the renamed path dependency, through Option::map on a std Vec's slice.
    assert (f"{cli}::main", f"{shape}::Circle.new") in edges
    assert (f"{cli}::main", f"{shape}::Circle.area") in edges
    assert (f"{cli}::main", f"{lib}::total") in edges
    assert (f"{cli}::main", f"{shape}::Square.area") not in edges  # same name, other type
    assert (f"{lib}::total", f"{shape}::Circle.area") in edges  # through slice iterators and a closure
    assert (f"{shape}::tests.unit", f"{shape}::Circle.new") in edges  # cfg(test) code is analyzed


@needs_rust
def test_rust_analyzer_timeout_falls_back_and_cleans_up() -> None:
    scratch = tempfile.mkdtemp()
    files, manifests = _files()
    config = indexers.Config(rust_analyzer=ANALYZER, rust_src=LIBRARY, timeout=0, scratch=scratch)
    resolved, outcomes = asyncio.run(indexers.resolve(config, files, manifests=manifests))
    assert resolved == {} and [(o.status, o.reason) for o in outcomes] == [("failed", "timeout")]
    assert list(Path(scratch).iterdir()) == []
