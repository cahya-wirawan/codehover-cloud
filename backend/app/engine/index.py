"""Reference graph and ranking from parser output (07 "Stage contracts": parse, rank).

Links are name-based only and labeled `name`, never presented as type-confirmed (07). A name that
matches several definitions produces one edge per candidate sharing an `ambiguity_group`, so the viewer
can show all candidates. Ranking is deterministic: in-degree with an entry-point boost and a test
downweight, ties broken by path and position.
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


def build_index(files: list[dict[str, Any]], paths: dict[str, str]) -> Index:
    index = Index()
    by_name: dict[str, list[str]] = defaultdict(list)
    for parsed in files:
        for symbol in parsed["symbols"]:
            key = _global(parsed["file_id"], symbol["symbol_key"])
            index.symbols[key] = IndexedSymbol(parsed["file_id"], paths[parsed["file_id"]], symbol)
            by_name[symbol["name"]].append(key)
    for parsed in files:
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
