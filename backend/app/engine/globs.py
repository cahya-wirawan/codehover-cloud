"""Bounded path globs for scope selection (07).

`PurePath.full_match` compiles globs to backtracking regular expressions, and a glob such as
`*a*a*a*a*b` against a long path takes exponential time; both the globs and the repository paths are
tenant-controlled, so a request could pin a worker.

Supported syntax: `**` matches zero or more whole segments; `*` matches any run of characters inside a
segment. `?` and character classes are rejected by validation. A segment pattern is split at `*` into
literal pieces matched greedily with `str.find` (linear, in C): prefix, suffix, then each middle piece
at its leftmost position, which is exact for `*`-only patterns. Whole paths are matched by dynamic
programming over segments. Segment results are memoized per selection, and every evaluated segment is
charged, with each cell of the path DP, to a `Budget`; a selection that exceeds it stops with `GlobBudgetExceeded`.
"""

from __future__ import annotations

from functools import lru_cache

# Work units per selection: one per dynamic-programming cell, plus one per distinct segment evaluation.
# The worst accepted selection takes about a second; ordinary scopes on 2000 files use well under 10%.
DEFAULT_BUDGET = 2_000_000


class GlobBudgetExceeded(Exception):
    """The scope is too expensive to evaluate."""


class Budget:
    def __init__(self, limit: int = DEFAULT_BUDGET) -> None:
        self.remaining = limit
        self.memo: dict[tuple[str, str], bool] = {}

    def charge(self, cost: int = 1) -> None:
        self.remaining -= cost
        if self.remaining < 0:
            raise GlobBudgetExceeded("scope too complex")


@lru_cache(maxsize=1024)
def _pieces(pattern: str) -> tuple[str, ...]:
    return tuple(pattern.split("*"))


def _segment_uncached(name: str, pattern: str) -> bool:
    pieces = _pieces(pattern)
    if len(pieces) == 1:
        return name == pattern
    first, last = pieces[0], pieces[-1]
    if len(name) < len(first) + len(last) or not name.startswith(first) or not name.endswith(last):
        return False
    position, end = len(first), len(name) - len(last)
    for piece in pieces[1:-1]:
        if not piece:
            continue
        found = name.find(piece, position, end)
        if found < 0:
            return False
        position = found + len(piece)
    return True


def _segment(name: str, pattern: str, budget: Budget) -> bool:
    key = (name, pattern)
    cached = budget.memo.get(key)
    if cached is None:
        budget.charge(1 + len(name) // 64)
        cached = budget.memo[key] = _segment_uncached(name, pattern)
    return cached


@lru_cache(maxsize=256)
def _parts(glob: str) -> tuple[str, ...]:
    return tuple(glob.split("/"))


def match(path: str, glob: str, budget: Budget | None = None) -> bool:
    """Whole-path match by dynamic programming over segments."""
    budget = budget or Budget()
    names = path.split("/")
    parts = _parts(glob)
    budget.charge(len(names) * len(parts))  # every DP cell, even when its segment result is memoized
    # reachable[j]: the names consumed so far can be matched by the first j pattern parts.
    reachable = [False] * (len(parts) + 1)
    reachable[0] = True
    for j, part in enumerate(parts):
        if part == "**" and reachable[j]:
            reachable[j + 1] = True
    for name in names:
        nxt = [False] * (len(parts) + 1)
        for j, part in enumerate(parts):
            if not reachable[j]:
                continue
            if part == "**":
                nxt[j] = True  # ** absorbs this segment and may absorb more
                nxt[j + 1] = True  # ** consumes exactly this segment
            elif _segment(name, part, budget):
                nxt[j + 1] = True
        for j, part in enumerate(parts):  # ** may also match zero further segments
            if part == "**" and nxt[j]:
                nxt[j + 1] = True
        reachable = nxt
    return reachable[len(parts)]
