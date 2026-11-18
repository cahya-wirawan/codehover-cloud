"""Linear-time path globs for scope selection (07).

`PurePath.full_match` compiles globs to backtracking regular expressions, and a glob such as
`*a*a*a*a*b` against a long path takes exponential time: a request could pin an API worker. This matcher
is polynomial and bounded: `**` matches zero or more whole segments, `*` any run of characters inside a
segment, `?` one character. Character classes are not supported (rejected by validation).
"""

from __future__ import annotations

from functools import lru_cache


def _segment(name: str, pattern: str) -> bool:
    """Classic two-pointer wildcard match: O(len(name) * len(pattern)) worst case, no backtracking blowup."""
    n = p = 0
    star = -1
    mark = 0
    while n < len(name):
        if p < len(pattern) and (pattern[p] == "?" or pattern[p] == name[n]):
            n += 1
            p += 1
        elif p < len(pattern) and pattern[p] == "*":
            star, mark = p, n
            p += 1
        elif star != -1:
            p = star + 1
            mark += 1
            n = mark
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


@lru_cache(maxsize=256)
def _parts(glob: str) -> tuple[str, ...]:
    return tuple(glob.split("/"))


def match(path: str, glob: str) -> bool:
    """Whole-path match. Dynamic programming over segments: O(segments * pattern parts * segment cost)."""
    names = path.split("/")
    parts = _parts(glob)
    # reachable[j]: the first i names can be matched by the first j pattern parts.
    reachable = [False] * (len(parts) + 1)
    reachable[0] = True
    for j, part in enumerate(parts):
        if part == "**":
            reachable[j + 1] = reachable[j + 1] or reachable[j]
    for name in names:
        nxt = [False] * (len(parts) + 1)
        for j, part in enumerate(parts):
            if not reachable[j]:
                continue
            if part == "**":
                nxt[j] = True  # ** absorbs this segment and may absorb more
                nxt[j + 1] = nxt[j + 1] or _segment(name, "*")
            elif _segment(name, part):
                nxt[j + 1] = True
        for j, part in enumerate(parts):  # ** may also match zero further segments
            if part == "**" and nxt[j]:
                nxt[j + 1] = True
        reachable = nxt
    return reachable[len(parts)]
