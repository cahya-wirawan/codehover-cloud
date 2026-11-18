"""Search terms and query normalization for build-scoped search (05 "Search documents", 06).

Identifiers are indexed whole and in parts: `parse_shape`, `parseShape` and `ParseShape` all add the
terms `parse` and `shape`. Queries become prefix tsqueries over sanitized words, so user input never
reaches `to_tsquery` syntax directly.
"""

from __future__ import annotations

import re

WORD = re.compile(r"[^\W_]+", re.UNICODE)
CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
MAX_QUERY_WORDS = 8
CITATION = re.compile(r"\[\[[^\]]*\]\]")


def identifier_terms(*names: str) -> str:
    terms: list[str] = []
    for name in names:
        for word in WORD.findall(name):
            parts = CAMEL.findall(word) if word.isascii() else [word]
            terms.extend(p.lower() for p in parts)
            terms.append(word.lower())
    return " ".join(dict.fromkeys(terms))[:4000]


def prose(markdown: str) -> str:
    """Guide text for the index: no citation markers, no fences' syntax noise."""
    text = CITATION.sub(" ", markdown)
    text = re.sub(r"[#>*`_|\[\]()]", " ", text)
    return " ".join(text.split())[:20000]


def tsquery(query: str) -> str | None:
    """`foo bar` -> `foo:* & bar:*`; None when nothing searchable remains."""
    words = [w.lower() for w in WORD.findall(query)][:MAX_QUERY_WORDS]
    words = [w for w in words if len(w) <= 64]
    return " & ".join(f"{w}:*" for w in words) or None
