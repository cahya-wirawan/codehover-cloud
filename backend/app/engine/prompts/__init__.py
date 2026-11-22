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


@lru_cache(maxsize=8)
def _load(version: str) -> dict[str, str]:
    """Every template of the set; a released directory is never edited (new text is a new version)."""
    directory = HERE / version
    return {path.stem: path.read_text() for path in sorted(directory.glob("*.txt"))}


@lru_cache(maxsize=4)
def digest(version: str) -> str:
    templates = _load(version)
    body = json.dumps(templates, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


DEFAULT_VARIANT = "en-standard"
# FR-026: the 30 most spoken languages by total speakers (Ethnologue 2026, as listed by Wikipedia's "List of
# languages by total number of speakers"), keyed by ISO 639-1 where one exists, else ISO 639-3. The names are
# what the audience instruction tells the writer; English, German and Indonesian keep the names of earlier
# releases, so their cached outputs stay valid.
LANGUAGES = {
    "en": "English",
    "zh": "Mandarin Chinese (Simplified Chinese characters)",
    "hi": "Hindi",
    "es": "Spanish",
    "ar": "Modern Standard Arabic",
    "fr": "French",
    "bn": "Bengali",
    "pt": "Portuguese",
    "id": "Indonesian",
    "ur": "Urdu",
    "ru": "Russian",
    "de": "German",
    "ja": "Japanese",
    "pcm": "Nigerian Pidgin",
    "arz": "Egyptian Arabic",
    "mr": "Marathi",
    "vi": "Vietnamese",
    "te": "Telugu",
    "sw": "Swahili",
    "ha": "Hausa",
    "tr": "Turkish",
    "pnb": "Western Punjabi (Shahmukhi script)",
    "tl": "Tagalog",
    "ta": "Tamil",
    "yue": "Cantonese (Traditional Chinese characters)",
    "wuu": "Wu Chinese (Shanghainese, Simplified Chinese characters)",
    "fa": "Persian",
    "ko": "Korean",
    "am": "Amharic",
    "th": "Thai",
}
LANGUAGE_PATTERN = "^(" + "|".join(LANGUAGES) + ")$"
LEVELS = ("standard", "beginner", "expert")


def variant(language: str, level: str) -> str:
    return f"{language}-{level}"


def system(version: str, audience: str = DEFAULT_VARIANT) -> str:
    """The set's system prompt; for any variant but English/standard, followed by the set's audience
    instruction (from 0.4.0 on). The audience text is fixed by the set, never taken from requests."""
    templates = _load(version)
    base = templates["system"].strip()
    if audience == DEFAULT_VARIANT:
        return base
    language, _, level = audience.partition("-")
    if "audience" not in templates or language not in LANGUAGES or level not in LEVELS:
        raise ValueError(f"prompt set {version} has no audience {audience!r}")
    instruction = string.Template(templates["audience"]).substitute(
        LANGUAGE=LANGUAGES[language], LEVEL=level, LEVEL_GUIDANCE=templates[f"level_{level}"].strip()
    )
    return f"{base}\n\n{instruction.strip()}"


def uses(version: str, name: str, placeholder: str) -> bool:
    """Whether a template of the set reads `$placeholder`; inputs it does not read stay out of cache keys,
    so a newer engine does not invalidate an older set's cached outputs."""
    return f"${placeholder}" in _load(version)[name]


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
