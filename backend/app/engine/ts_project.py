"""TypeScript projects for scip-typescript from a repository's path aliases (T23, 07).

Runs inside the sandboxed index worker: the repository's `tsconfig*.json` / `jsconfig*.json` files are
data here. Only `compilerOptions.baseUrl` and `compilerOptions.paths` are read, through `extends` chains
that stay inside the snapshot (a package name such as `@tsconfig/node20` is skipped). Plugins, project
references, `include`/`exclude` and every other option are ignored; the indexer's own options
(`BASE_OPTIONS`) always apply. Every target must stay inside the checkout.

Each source file belongs to the project of its nearest `tsconfig.json` / `jsconfig.json`, as in an
editor. A project whose aliases are empty joins the plain project, which has no aliases. With no aliases
anywhere there are no projects, and the indexer runs exactly as before (one project of all files).
"""

from __future__ import annotations

import json
import posixpath
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


def is_config(path: str) -> bool:
    name = posixpath.basename(path)
    return name.endswith(".json") and name.startswith(("tsconfig", "jsconfig"))


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
    """`baseUrl` (checkout-relative) and `paths` with the directory they are relative to, following
    `extends` like TypeScript: later bases override earlier ones, the config itself overrides all."""
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


def _owner(path: str, roots: set[str]) -> str | None:
    directory = posixpath.dirname(path)
    while True:
        if directory in roots:
            return directory
        if not directory:
            return None
        directory = posixpath.dirname(directory)


def projects(configs: dict[str, str], files: list[str], src: str) -> list[dict[str, Any]]:
    """Generated tsconfig contents, one per aliasing project plus the plain one, with absolute paths
    under `src` (the configs are written outside the checkout). Empty when no project has aliases."""
    configs = {p: t for p, t in list(sorted(configs.items()))[:MAX_CONFIGS] if len(t) <= MAX_CONFIG_BYTES}
    roots = {posixpath.dirname(p): p for p in configs if posixpath.basename(p) in PROJECT_NAMES}
    settings = {}
    for directory, path in roots.items():
        base_url, aliases = _aliases(_options(path, configs))
        if base_url is not None or aliases:
            settings[directory] = (base_url, aliases)
    if not settings:
        return []
    groups: dict[str | None, list[str]] = {}
    for path in sorted(files):
        owner = _owner(path, set(roots))
        groups.setdefault(owner if owner in settings else None, []).append(path)

    def absolute(path: str) -> str:
        return posixpath.join(src, path) if path else src

    result = []
    for owner in sorted((o for o in groups if o is not None), key=str):
        base_url, aliases = settings[owner]
        options = dict(BASE_OPTIONS)
        if base_url is not None:
            options["baseUrl"] = absolute(base_url)
        if aliases:
            options["paths"] = {k: [absolute(t) for t in v] for k, v in aliases.items()}
        result.append({"compilerOptions": options, "files": [absolute(p) for p in groups[owner]]})
    if None in groups:
        result.append({"compilerOptions": dict(BASE_OPTIONS), "files": [absolute(p) for p in groups[None]]})
    return result
