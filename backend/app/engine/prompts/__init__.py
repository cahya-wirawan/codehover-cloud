"""Versioned prompt sets (19, 23): one immutable directory per prompt-set version, `prompts/<X.Y.Z>/`.

Templates use `$NAME` placeholders (string.Template), so repository data inserted into them is never
parsed again. Every data section is wrapped by `data_block` between BEGIN/END lines that carry an id
derived from the content's SHA-256: data cannot forge its own end marker without a hash fixed point.
Size limits are enforced by the callers before interpolation. `digest()` hashes every template of the
set; it goes into cache keys and provenance, so any text change invalidates cached outputs.
"""

from __future__ import annotations

import hashlib
import json
import string
from functools import lru_cache
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TEMPLATES = ("system", "explain", "overview", "plan", "guide", "repair")


@lru_cache(maxsize=4)
def _load(version: str) -> dict[str, str]:
    directory = HERE / version
    return {name: (directory / f"{name}.txt").read_text() for name in TEMPLATES}


@lru_cache(maxsize=4)
def digest(version: str) -> str:
    templates = _load(version)
    body = json.dumps(templates, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def system(version: str) -> str:
    return _load(version)["system"].strip()


def render(version: str, name: str, **sections: str) -> str:
    """Fill a template; every value must already be a `data_block` or a plain value we produced."""
    return string.Template(_load(version)[name]).substitute(sections).strip()


def data_block(name: str, content: str) -> str:
    marker = hashlib.sha256(content.encode()).hexdigest()[:16]
    return f"<<<BEGIN {name} id={marker}>>>\n{content}\n<<<END {name} id={marker}>>>"


def json_block(name: str, value: Any) -> str:
    return data_block(name, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1))


def numbered(lines: list[str], start_line: int) -> str:
    width = len(str(start_line + len(lines)))
    return "\n".join(f"{start_line + i:>{width}} | {line}" for i, line in enumerate(lines))
