"""Reference graph and ranking from parser output (07 "Stage contracts": parse, rank).

A file a SCIP indexer resolved (app.engine.indexers, T23) gets `index` edges: each reference goes to the
definition the type checker found, mapped onto the parser's symbol of that name whose span contains the
indexer's definition. Every other file keeps name matching, labeled `name` and never presented as
type-confirmed (07): a name that matches several definitions produces one edge per candidate sharing an
`ambiguity_group`, so the viewer can show all candidates. Ranking is deterministic: in-degree with an
entry-point boost and a test downweight, ties broken by path and position.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

ENTRY_NAMES = frozenset({"main", "index", "app", "cli", "__main__", "server", "manage"})


@dataclass
class IndexedSymbol:
    file_id: str
    path: str
    data: dict[str, Any]
    rank: float = 0.0
    in_degree: int = 0


@dataclass(frozen=True)
class Edge:
    from_key: str | None  # (file_id, symbol_key) of the enclosing symbol, or None at module level
    to_key: str
    file_id: str
    line: int
    column: int
    ambiguity_group: str | None
    resolution: str = "name"  # "index" when a SCIP indexer resolved the reference


@dataclass
class Index:
    symbols: dict[str, IndexedSymbol] = field(default_factory=dict)  # global key "file_id::symbol_key"
    edges: list[Edge] = field(default_factory=list)


def _global(file_id: str, symbol_key: str) -> str:
    return f"{file_id}::{symbol_key}"


def _enclosing(symbols: list[dict[str, Any]], line: int, column: int) -> dict[str, Any] | None:
    best = None
    for s in symbols:
        starts = (s["start_line"], s["start_column"]) <= (line, column)
        ends = (line, column) <= (s["end_line"], s["end_column"])
        if starts and ends and (best is None or s["start_line"] >= best["start_line"]):
            best = s
    return best


def _definition(symbols: list[dict[str, Any]], line: int, column: int, name: str) -> dict[str, Any] | None:
    """The innermost parser symbol called `name` whose span contains an indexer's definition position."""
    candidates = [
        s
        for s in symbols
        if s["name"] == name
        and (s["start_line"], s["start_column"]) <= (line, column) <= (s["end_line"], s["end_column"])
    ]
    return max(candidates, key=lambda s: (s["start_line"], s["start_column"]), default=None)


def build_index(
    files: list[dict[str, Any]], paths: dict[str, str], resolved: dict[str, dict[str, Any]] | None = None
) -> Index:
    """`resolved` maps file IDs to indexer occurrences (app.engine.indexers.resolve):
    definitions as (line, column, name, symbol), references as (line, column, symbol)."""
    index = Index()
    resolved = resolved or {}
    by_name: dict[str, list[str]] = defaultdict(list)
    for parsed in files:
        for symbol in parsed["symbols"]:
            key = _global(parsed["file_id"], symbol["symbol_key"])
            index.symbols[key] = IndexedSymbol(parsed["file_id"], paths[parsed["file_id"]], symbol)
            by_name[symbol["name"]].append(key)
    defined_by: dict[str, list[str]] = defaultdict(list)  # indexer symbol -> parser symbols it defines
    for parsed in files:
        for line, column, name, symbol in resolved.get(parsed["file_id"], {}).get("definitions", []):
            found = _definition(parsed["symbols"], line, column, name)
            if found is None:
                continue  # parameters, variables, modules: not symbols the product shows
            defined = _global(parsed["file_id"], found["symbol_key"])
            if defined not in defined_by[symbol]:
                defined_by[symbol].append(defined)
    for parsed in files:
        occurrences = resolved.get(parsed["file_id"])
        if occurrences is not None:
            for line, column, symbol in occurrences.get("references", []):
                candidates = defined_by.get(symbol, [])
                enclosing = _enclosing(parsed["symbols"], line, column)
                from_key = _global(parsed["file_id"], enclosing["symbol_key"]) if enclosing else None
                # Several parser symbols for one indexer symbol: a redefinition. All are candidates.
                group = index.symbols[candidates[0]].data["name"] if len(candidates) > 1 else None
                for target in candidates:
                    if target == from_key:
                        continue
                    index.edges.append(
                        Edge(from_key, target, parsed["file_id"], line, column, group, "index")
                    )
                    index.symbols[target].in_degree += 1
            continue
        for ref in parsed["references"]:
            targets = by_name.get(ref["name"], [])
            if not targets:
                continue
            enclosing = _enclosing(parsed["symbols"], ref["line"], ref["column"])
            from_key = _global(parsed["file_id"], enclosing["symbol_key"]) if enclosing else None
            group = ref["name"] if len(targets) > 1 else None
            for target in targets:
                if target == from_key:
                    continue  # recursion is not a dependency on oneself
                index.edges.append(
                    Edge(from_key, target, parsed["file_id"], ref["line"], ref["column"], group)
                )
                index.symbols[target].in_degree += 1
    peak = max((s.in_degree for s in index.symbols.values()), default=0) or 1
    for symbol in index.symbols.values():
        path = PurePosixPath(symbol.path)
        score = symbol.in_degree / peak
        if path.stem in ENTRY_NAMES:
            score = score * 1.2 + 0.05
        if "test" in path.as_posix().lower():
            score *= 0.5
        symbol.rank = round(score, 6)
    return index
