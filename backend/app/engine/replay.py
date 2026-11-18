"""Deterministic replay explanations (07 "Provider abstraction": replay adapter; 23 prompt set 0.1.0).

No model is called. Each card is derived from the parsed source only (docstring, signature,
parameters), so a build is reproducible and free. Output is validated against the explanation schema v2
contract, the same validator real model output must pass (19).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from app.engine.index import IndexedSymbol

GENERATOR = "replay-v1"
SCHEMA_PATH = Path(__file__).with_name("schemas") / "explanation.schema.json"


@lru_cache(maxsize=1)
def validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))


def _first_sentence(text: str, limit: int) -> str:
    sentence = text.strip().split("\n\n")[0].replace("\n", " ")
    end = sentence.find(". ")
    return (sentence if end == -1 else sentence[: end + 1])[:limit]


def card(symbol: IndexedSymbol) -> dict[str, Any]:
    data = symbol.data
    docstring = data.get("docstring")
    kind = str(data["kind"])
    summary = (
        _first_sentence(docstring, 600)
        if docstring
        else f"{kind.capitalize()} `{data['name']}` defined in `{symbol.path}`."
    )
    details = f"Signature: `{data['signature']}`" if data.get("signature") else ""
    return {
        "symbol_key": data["symbol_key"],
        "summary": summary,
        "details": details[:3000],
        "params": [{"name": p, "description": f"Parameter `{p}`."} for p in data.get("params", [])[:100]],
        "returns": "",
        "notes": "AI generation is disabled for replay builds; this card is derived from the source only.",
    }


def explain_file(path: str, symbols: list[IndexedSymbol]) -> dict[str, Any]:
    names = ", ".join(f"`{s.data['name']}`" for s in symbols[:8])
    explanation = {
        "summary": (f"`{path}` defines {len(symbols)} symbol(s): {names}." if symbols else f"`{path}`.")[
            :2000
        ],
        "symbols": [card(s) for s in symbols[:200]],
        "notes": [],
    }
    errors = sorted(validator().iter_errors(explanation), key=lambda e: list(e.path))
    if errors:
        raise ValueError(f"explanation schema violation: {errors[0].message}")
    return explanation
