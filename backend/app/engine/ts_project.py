"""TypeScript projects for scip-typescript from a repository's configs (T23, 07).

Runs inside the sandboxed index worker: the repository's `tsconfig*.json` / `jsconfig*.json` and
`package.json` files are data here. From a config only these are read, through `extends` chains that stay
inside the snapshot (a package name such as `@tsconfig/node20` is skipped):
- `compilerOptions.baseUrl`, `paths`, `rootDirs` and `moduleSuffixes` (module resolution);
- `files`, `include` and `exclude`, which say which files a config owns, and `references`, which make the
  referenced configs projects too (a solution-style `tsconfig.json` with `"files": []` that references
  `tsconfig.app.json` and `tsconfig.node.json`, as Vite projects do).
Plugins and every other option are ignored; the indexer's own options (`BASE_OPTIONS`) always apply.
Every target must stay inside the checkout.

Workspace packages: a `package.json` with a `name` inside the checkout makes that name resolve to the
package's source entry (`source`, `types`, `exports`, `module` or `main` when that file is in the checkout,
also after mapping `dist/`, `lib/`, `build/` or `out/` to `src/` and `.js` to `.ts`; else `src/index.ts`
and the like), and `name/*` to its `src` (or its directory). They apply to every project, under each
project's own `paths`: installed packages are never in the checkout, so a monorepo's imports of its own
packages by name would otherwise stay unresolved.

Each source file belongs to its nearest directory with a config: there, the first config (the
`tsconfig.json` / `jsconfig.json`, then the configs it references in that directory) that owns it by
`files` / `include` / `exclude`. A config with nothing to resolve, or a file no config owns, joins the
plain project. With nothing to resolve anywhere there are no projects, and the indexer runs exactly as
before (one project of all files).
"""

from __future__ import annotations

import json
import posixpath
import re
from typing import Any

BASE_OPTIONS: dict[str, Any] = {
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
}
PROJECT_NAMES = frozenset({"tsconfig.json", "jsconfig.json"})
MAX_CONFIGS = 200
MAX_CONFIG_BYTES = 256 * 1024
MAX_EXTENDS_DEPTH = 8
MAX_ALIASES = 200
MAX_TARGETS = 10
MAX_PATTERN_CHARS = 300
MAX_REFERENCES = 50
MAX_GLOBS = 50
MAX_PACKAGES = 200
MAX_ROOT_DIRS = 20
MAX_SUFFIXES = 10
SUFFIX = re.compile(r"^[.A-Za-z0-9_-]{0,20}$")
PACKAGE_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._~-]{0,100}/)?[a-z0-9][a-z0-9._~-]{0,100}$")
SOURCE_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".d.ts", ".js", ".jsx", ".mjs", ".cjs")
ENTRY_GUESSES = ("src/index", "index", "lib/index", "src/main", "main")
BUILD_DIRS = ("dist/", "lib/", "build/", "out/")
SCRIPT_SUFFIX = re.compile(r"(\.d)?\.[cm]?[jt]sx?$")


def is_config(path: str) -> bool:
    name = posixpath.basename(path)
    return name.endswith(".json") and name.startswith(("tsconfig", "jsconfig"))


def is_package(path: str) -> bool:
    return posixpath.basename(path) == "package.json"


def parse_jsonc(text: str) -> Any:
    """JSON with comments and trailing commas, as tsconfig files are written; None if not that."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "]}":
            k = len(out) - 1
            while k >= 0 and out[k].isspace():
                k -= 1
            if k >= 0 and out[k] == ",":
                del out[k]
            out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
    try:
        return json.loads("".join(out))
    except ValueError:
        return None


def _inside(path: str) -> str | None:
    """A checkout-relative path, or None when it would leave the checkout."""
    if path.startswith("/") or "\\" in path or "\0" in path:
        return None
    normal = posixpath.normpath(path)
    if normal == ".." or normal.startswith("../"):
        return None
    return "" if normal == "." else normal


def _extends(directory: str, value: str, configs: dict[str, str]) -> str | None:
    if not value.startswith(("./", "../")):
        return None  # a package's config (node_modules is never in the checkout)
    target = _inside(posixpath.join(directory, value))
    if target is None:
        return None
    for candidate in (target, f"{target}.json", posixpath.join(target, "tsconfig.json")):
        if candidate in configs:
            return candidate
    return None


def _options(path: str, configs: dict[str, str], seen: frozenset[str] = frozenset()) -> dict[str, Any]:
    """What a config resolves and owns, following `extends` like TypeScript: later bases override
    earlier ones, the config itself overrides all; paths stay relative to the config that set them."""
    data = parse_jsonc(configs[path])
    if not isinstance(data, dict) or len(seen) >= MAX_EXTENDS_DEPTH:
        return {}
    directory = posixpath.dirname(path)
    result: dict[str, Any] = {}
    bases = data.get("extends")
    for value in bases if isinstance(bases, list) else [bases]:
        if isinstance(value, str):
            base = _extends(directory, value, configs)
            if base is not None and base not in seen and base != path:
                result.update(_options(base, configs, seen | {path}))
    options = data.get("compilerOptions")
    if isinstance(options, dict):
        if isinstance(options.get("baseUrl"), str):
            base_url = _inside(posixpath.join(directory, options["baseUrl"]))
            if base_url is not None:
                result["baseUrl"] = base_url
        if isinstance(options.get("paths"), dict):
            result["paths"], result["pathsDir"] = options["paths"], directory
        if isinstance(options.get("rootDirs"), list):
            dirs = (
                _inside(posixpath.join(directory, d))
                for d in options["rootDirs"][:MAX_ROOT_DIRS]
                if isinstance(d, str) and d and len(d) <= MAX_PATTERN_CHARS
            )
            result["rootDirs"] = [d for d in dirs if d is not None]
        if isinstance(options.get("moduleSuffixes"), list):
            result["moduleSuffixes"] = [
                x for x in options["moduleSuffixes"][:MAX_SUFFIXES] if isinstance(x, str) and SUFFIX.match(x)
            ]
    for key in ("files", "include", "exclude"):
        if isinstance(data.get(key), list):
            result[key] = (directory, data[key])
    return result


def _aliases(options: dict[str, Any]) -> tuple[str | None, dict[str, list[str]]]:
    """The effective baseUrl and alias patterns, every target checkout-relative."""
    base_url = options.get("baseUrl")
    raw = options.get("paths") or {}
    relative_to = base_url if base_url is not None else options.get("pathsDir", "")
    aliases: dict[str, list[str]] = {}
    for pattern, targets in list(raw.items())[:MAX_ALIASES]:
        if (
            not isinstance(pattern, str)
            or not pattern
            or len(pattern) > MAX_PATTERN_CHARS
            or pattern.count("*") > 1
            or not isinstance(targets, list)
        ):
            continue
        kept = []
        for target in targets[:MAX_TARGETS]:
            if (
                isinstance(target, str)
                and target
                and len(target) <= MAX_PATTERN_CHARS
                and target.count("*") <= 1
            ):
                resolved = _inside(posixpath.join(relative_to, target))
                if resolved:
                    kept.append(resolved)
        if kept:
            aliases[pattern] = kept
    return base_url, aliases


def _glob(directory: str, pattern: str) -> re.Pattern[str] | None:
    """A tsconfig `include`/`exclude` pattern as a regex over checkout paths. A last segment without a
    wildcard or an extension names a directory, which means everything under it."""
    if not pattern or len(pattern) > MAX_PATTERN_CHARS:
        return None
    path = _inside(posixpath.join(directory, pattern))
    if path is None:
        return None
    last = posixpath.basename(path)
    if not any(c in last for c in "*?") and "." not in last:
        path = posixpath.join(path, "**/*") if path else "**/*"
    out, i = [], 0
    while i < len(path):
        if path.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif path.startswith("**", i):
            out.append(".*")
            i += 2
        elif path[i] == "*":
            out.append("[^/]*")
            i += 1
        elif path[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(path[i]))
            i += 1
    return re.compile("".join(out))


def _globs(directory: str, patterns: list[Any]) -> list[re.Pattern[str]]:
    found = (_glob(directory, p) for p in patterns[:MAX_GLOBS] if isinstance(p, str))
    return [g for g in found if g is not None]


class _Owned:
    """The files a config owns: its `files`, plus its `include` (by default everything under it) less its
    `exclude`. A config with `"files": []` and no `include` (a solution) owns none."""

    def __init__(self, path: str, options: dict[str, Any]) -> None:
        directory = posixpath.dirname(path)
        files_dir, files = options.get("files", (directory, None))
        include_dir, include = options.get("include", (directory, None))
        exclude_dir, exclude = options.get("exclude", (directory, []))
        listed = (
            _inside(posixpath.join(files_dir, f)) for f in (files or [])[:MAX_CONFIGS] if isinstance(f, str)
        )
        self.files = {p for p in listed if p}
        if include is None:
            include = [] if files is not None else ["**/*"]
        self.include = _globs(include_dir, include)
        self.exclude = _globs(exclude_dir, exclude)

    def owns(self, path: str) -> bool:
        if path in self.files:
            return True
        return any(g.fullmatch(path) for g in self.include) and not any(
            g.fullmatch(path) for g in self.exclude
        )


def _references(path: str, configs: dict[str, str]) -> list[str]:
    """The configs a config references (`references: [{"path": ...}]`) that are in the snapshot."""
    data = parse_jsonc(configs[path])
    refs = data.get("references") if isinstance(data, dict) else None
    found = []
    for ref in refs[:MAX_REFERENCES] if isinstance(refs, list) else []:
        value = ref.get("path") if isinstance(ref, dict) else None
        target = _inside(posixpath.join(posixpath.dirname(path), value)) if isinstance(value, str) else None
        if target is None:
            continue
        for candidate in (target, posixpath.join(target, "tsconfig.json")):
            if candidate in configs and candidate != path:
                found.append(candidate)
                break
    return found


def _entry(directory: str, data: dict[str, Any], files: set[str]) -> str | None:
    """A workspace package's source entry in the checkout, or None."""
    candidates: list[str] = [data[k] for k in ("source", "types", "typings") if isinstance(data.get(k), str)]
    exports = data.get("exports")
    root = exports.get(".", exports) if isinstance(exports, dict) else exports
    if isinstance(root, str):
        candidates.append(root)
    elif isinstance(root, dict):
        for key in ("source", "types", "import", "default", "require"):
            value = root.get(key)
            if isinstance(value, dict):
                value = value.get("default")
            if isinstance(value, str):
                candidates.append(value)
    candidates += [data[k] for k in ("module", "main") if isinstance(data.get(k), str)]
    for candidate in candidates:
        path = _inside(posixpath.join(directory, candidate)) if len(candidate) <= MAX_PATTERN_CHARS else None
        if not path:
            continue
        relative = path[len(directory) + 1 :] if directory else path
        variants = [path] + [
            posixpath.join(directory, "src", relative[len(build) :])
            for build in BUILD_DIRS
            if relative.startswith(build)
        ]
        for variant in list(variants):
            stem = SCRIPT_SUFFIX.sub("", variant)
            variants += [stem + ext for ext in SOURCE_EXTENSIONS]
        for variant in variants:
            if variant in files:
                return variant
    for guess in ENTRY_GUESSES:
        for ext in SOURCE_EXTENSIONS:
            path = posixpath.join(directory, guess + ext)
            if path in files:
                return path
    return None


def workspace_aliases(packages: dict[str, str], files: list[str]) -> dict[str, list[str]]:
    """Path aliases for the checkout's own packages by name (monorepo workspaces)."""
    present = set(files)
    aliases: dict[str, list[str]] = {}
    for path, text in list(sorted(packages.items()))[:MAX_PACKAGES]:
        if not is_package(path) or len(text) > MAX_CONFIG_BYTES:
            continue
        data = parse_jsonc(text)
        name = data.get("name") if isinstance(data, dict) else None
        directory = _inside(posixpath.dirname(path))
        if not isinstance(name, str) or not PACKAGE_NAME.match(name) or directory is None or name in aliases:
            continue
        entry = _entry(directory, data, present)
        if entry:
            aliases[name] = [entry]
        source = posixpath.join(directory, "src") if directory else "src"
        under = source if any(f.startswith(source + "/") for f in present) else directory
        if under:
            aliases[f"{name}/*"] = [f"{under}/*"]
    return aliases


def _roots(configs: dict[str, str]) -> dict[str, list[str]]:
    """Per directory, its configs in the order they claim files: the `tsconfig.json` / `jsconfig.json`,
    then the configs it references there (transitively); a referenced config in a directory without one
    makes that directory's list."""
    roots: dict[str, list[str]] = {}

    def expand(start: str) -> list[str]:
        order, queue = [start], [start]
        while queue and len(order) < MAX_REFERENCES:
            for ref in _references(queue.pop(0), configs):
                if ref not in order and posixpath.dirname(ref) == posixpath.dirname(start):
                    order.append(ref)
                    queue.append(ref)
        return order

    for path in configs:
        if posixpath.basename(path) in PROJECT_NAMES:
            roots.setdefault(posixpath.dirname(path), expand(path))
    for path in list(configs):
        for ref in _references(path, configs):
            roots.setdefault(posixpath.dirname(ref), expand(ref))
    return roots


def _nearest(path: str, roots: dict[str, list[str]]) -> str | None:
    directory = posixpath.dirname(path)
    while True:
        if directory in roots:
            return directory
        if not directory:
            return None
        directory = posixpath.dirname(directory)


def projects(
    configs: dict[str, str], files: list[str], src: str, packages: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Generated tsconfig contents, one per project with something to resolve plus the plain one, with
    absolute paths under `src` (the configs are written outside the checkout). Empty when nothing
    resolves anywhere."""
    configs = {p: t for p, t in list(sorted(configs.items()))[:MAX_CONFIGS] if len(t) <= MAX_CONFIG_BYTES}
    shared = workspace_aliases(packages or {}, files)
    roots = _roots(configs)
    options = {path: _options(path, configs) for paths in roots.values() for path in paths}
    owned = {path: _Owned(path, o) for path, o in options.items()}
    settings: dict[str, tuple[str | None, dict[str, list[str]], dict[str, Any]]] = {}
    for path, o in options.items():
        base_url, aliases = _aliases(o)
        extra = {k: o[k] for k in ("rootDirs", "moduleSuffixes") if o.get(k)}
        if base_url is not None or aliases or extra:
            settings[path] = (base_url, aliases, extra)
    if not settings and not shared:
        return []
    groups: dict[str | None, list[str]] = {}
    for path in sorted(files):
        nearest = _nearest(path, roots)
        owner = (
            next((c for c in roots[nearest] if owned[c].owns(path)), None) if nearest is not None else None
        )
        groups.setdefault(owner if owner in settings else None, []).append(path)

    def absolute(path: str) -> str:
        return posixpath.join(src, path) if path else src

    def compiler_options(
        base_url: str | None, aliases: dict[str, list[str]], extra: dict[str, Any]
    ) -> dict[str, Any]:
        result = dict(BASE_OPTIONS)
        if base_url is not None:
            result["baseUrl"] = absolute(base_url)
        merged = {**shared, **aliases}  # a project's own paths win over the workspace packages
        if merged:
            result["paths"] = {k: [absolute(t) for t in v] for k, v in merged.items()}
        if extra.get("rootDirs"):
            result["rootDirs"] = [absolute(d) for d in extra["rootDirs"]]
        if extra.get("moduleSuffixes"):
            result["moduleSuffixes"] = extra["moduleSuffixes"]
        return result

    result = []
    for owner in sorted((o for o in groups if o is not None), key=lambda p: (posixpath.dirname(p), p)):
        result.append(
            {
                "compilerOptions": compiler_options(*settings[owner]),
                "files": [absolute(p) for p in groups[owner]],
            }
        )
    if None in groups:
        result.append(
            {"compilerOptions": compiler_options(None, {}, {}), "files": [absolute(p) for p in groups[None]]}
        )
    return result
