"""The crate graph rust-analyzer indexes Rust with (T23), built from the repository's Cargo manifests.

Runs inside the sandbox's indexer profile (app.engine.index_worker), because manifests are repository
content. The result is a `rust-project.json`: rust-analyzer then never runs cargo, so no build script,
proc macro or cargo subcommand of the repository ever runs, and nothing is downloaded. The graph follows
Cargo's target discovery (`src/lib.rs`, `src/main.rs`, `src/bin`, `examples`, `tests`, `benches`,
`build.rs` and explicit `path`s) and links crates through path or workspace dependencies between the
repository's own packages. Dependencies from crates.io are absent, so references into them stay
unresolved. Every declared feature and `cfg(test)` are enabled so that feature-gated and test code is
analyzed too. Only files of the scratch checkout become crate roots.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

EDITIONS = frozenset({"2015", "2018", "2021", "2024"})
MAX_MANIFESTS = 500
MAX_CRATES = 2000
DEPENDENCY_TABLES = ("dependencies", "dev-dependencies", "build-dependencies")
# Cargo's own target cfgs for the worker's platform family, so `#[cfg(unix)]` code is analyzed.
TARGET_CFG = (
    "unix",
    'target_family="unix"',
    'target_os="linux"',
    'target_env="gnu"',
    'target_pointer_width="64"',
    'target_endian="little"',
    'panic="unwind"',
    "debug_assertions",
    "test",
)


@dataclass
class _Package:
    directory: PurePosixPath
    name: str
    edition: str
    features: list[str]
    manifest: dict[str, Any]
    dependencies: dict[str, dict[str, str]] = field(default_factory=dict)  # table -> extern name -> package
    lib: int | None = None  # index of the package's library crate


def _table(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _crate_name(name: str) -> str:
    return name.replace("-", "_")


def _dependencies(manifest: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Per table, extern name -> package name (`package = "..."` renames), target tables included."""
    tables: list[tuple[str, dict[str, Any]]] = [(t, _table(manifest.get(t))) for t in DEPENDENCY_TABLES]
    for target in _table(manifest.get("target")).values():
        tables += [(t, _table(_table(target).get(t))) for t in DEPENDENCY_TABLES]
    found: dict[str, dict[str, str]] = {t: {} for t in DEPENDENCY_TABLES}
    for table, entries in tables:
        for key, spec in entries.items():
            if not isinstance(key, str):
                continue
            package = _string(_table(spec).get("package")) or key
            found[table][_crate_name(key)] = package
    return found


def _edition(
    package: dict[str, Any], workspace_editions: list[tuple[PurePosixPath, str]], here: PurePosixPath
) -> str:
    edition = package.get("edition")
    if isinstance(edition, str) and edition in EDITIONS:
        return edition
    if _table(edition).get("workspace") is True:  # `edition.workspace = true`: the nearest workspace root
        for root, inherited in sorted(workspace_editions, key=lambda item: -len(item[0].parts)):
            if here == root or root in here.parents or root == PurePosixPath("."):
                return inherited
        return "2021"
    return "2015"  # Cargo's default when a package names no edition


def _targets(package: _Package, files: set[str]) -> list[tuple[str, str, str]]:
    """(kind, crate name, root path) for each target whose root file is in the checkout."""
    base, manifest = package.directory, package.manifest

    def path(relative: str) -> str:
        return str(base / relative) if str(base) != "." else relative

    def present(relative: Any) -> str | None:
        if not isinstance(relative, str) or ".." in PurePosixPath(relative).parts or relative.startswith("/"):
            return None
        candidate = path(relative)
        return candidate if candidate in files else None

    targets: list[tuple[str, str, str]] = []
    lib = _table(manifest.get("lib"))
    lib_root = present(lib.get("path", "src/lib.rs"))
    if lib_root:
        targets.append(("lib", _crate_name(_string(lib.get("name")) or package.name), lib_root))
    explicit: set[str] = set()
    for kind, table, directory in (
        ("bin", "bin", "src/bin"),
        ("example", "example", "examples"),
        ("test", "test", "tests"),
        ("bench", "bench", "benches"),
    ):
        entries = manifest.get(table)
        for entry in entries if isinstance(entries, list) else []:
            name = _string(_table(entry).get("name"))
            root = present(_table(entry).get("path") or (f"{directory}/{name}.rs" if name else None))
            if name and root:
                targets.append((kind, _crate_name(name), root))
                explicit.add(root)
        prefix = path(directory) + "/"
        for candidate in sorted(files):  # Cargo's auto-discovery: `dir/x.rs` and `dir/x/main.rs`
            if not candidate.startswith(prefix) or candidate in explicit:
                continue
            rest = PurePosixPath(candidate[len(prefix) :])
            if len(rest.parts) == 1 and rest.suffix == ".rs":
                targets.append((kind, _crate_name(rest.stem), candidate))
            elif len(rest.parts) == 2 and rest.name == "main.rs":
                targets.append((kind, _crate_name(rest.parts[0]), candidate))
    main = present("src/main.rs")
    if main and main not in explicit:
        targets.append(("bin", _crate_name(package.name), main))
    build = _table(manifest.get("package")).get("build", "build.rs")
    build_root = present(build) if build is not False else None
    if build_root:
        targets.append(("build", "build_script_build", build_root))
    return targets


def project(
    manifests: dict[str, str], files: list[str], work_src: str, sysroot_src: str | None
) -> dict[str, Any]:
    """A `rust-project.json` for the checkout. `manifests` maps `Cargo.toml` paths to their text; `files`
    are the checkout's `.rs` paths; root modules are absolute under `work_src`."""
    present = set(files)
    workspace_editions: list[tuple[PurePosixPath, str]] = []
    packages: list[_Package] = []
    for manifest_path, text in sorted(manifests.items())[:MAX_MANIFESTS]:
        try:
            manifest = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, ValueError):
            continue  # an unreadable manifest only leaves its package unresolved
        directory = PurePosixPath(manifest_path).parent
        inherited = _table(_table(manifest.get("workspace")).get("package")).get("edition")
        if isinstance(inherited, str) and inherited in EDITIONS:
            workspace_editions.append((directory, inherited))
        table = _table(manifest.get("package"))
        name = _string(table.get("name"))
        if name is None:
            continue  # a virtual workspace manifest
        features = [f for f in _table(manifest.get("features")) if isinstance(f, str)]
        packages.append(_Package(directory, name, "", features, manifest, _dependencies(manifest)))
    for package in packages:
        package.edition = _edition(
            _table(package.manifest.get("package")), workspace_editions, package.directory
        )

    crates: list[dict[str, Any]] = []
    roots: set[str] = set()
    planned: list[tuple[_Package, str, str, str]] = []
    for package in packages:
        for kind, name, root in _targets(package, present):
            if root in roots or len(planned) >= MAX_CRATES:
                continue  # one crate per root file
            roots.add(root)
            planned.append((package, kind, name, root))
    for package, kind, name, root in planned:
        if kind == "lib":
            package.lib = len(crates)
        crates.append(
            {
                "display_name": name,
                "root_module": f"{work_src}/{root}",
                "edition": package.edition,
                "deps": [],
                "cfg": [*TARGET_CFG, *(f'feature="{f}"' for f in package.features)],
                "is_workspace_member": True,
                "is_proc_macro": False,
            }
        )
    libraries = {p.name: p for p in packages if p.lib is not None}
    for index, (package, kind, _name, _root) in enumerate(planned):
        tables = {"lib": ("dependencies",), "build": ("build-dependencies",)}.get(
            kind, ("dependencies", "dev-dependencies")
        )
        deps: dict[str, int] = {}
        if kind not in ("lib", "build") and package.lib is not None:
            deps[crates[package.lib]["display_name"]] = package.lib  # a binary or test uses its own library
        for kind_table in tables:
            for extern, dependency in package.dependencies[kind_table].items():
                target = libraries.get(dependency)
                if target is not None and target.lib is not None and target.lib != index:
                    deps.setdefault(extern, target.lib)
        crates[index]["deps"] = [{"crate": crate, "name": extern} for extern, crate in deps.items()]
    if not crates:  # no Cargo package: lone crate roots still index on their own
        for root in sorted(r for r in present if PurePosixPath(r).name in ("lib.rs", "main.rs")):
            crates.append(
                {
                    "display_name": _crate_name(PurePosixPath(root).parent.name or "main"),
                    "root_module": f"{work_src}/{root}",
                    "edition": "2021",
                    "deps": [],
                    "cfg": list(TARGET_CFG),
                    "is_workspace_member": True,
                    "is_proc_macro": False,
                }
            )
            if len(crates) >= MAX_CRATES:
                break
    result: dict[str, Any] = {"crates": crates}
    if sysroot_src:
        result["sysroot_src"] = sysroot_src
    return result
