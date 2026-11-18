"""Generation scope and preflight estimate (07 "estimate/approve", 01 FR-006/FR-007).

`select_files` is the single scope function: the estimate counts exactly the files the build will
explain, so an approved estimate fixes the scope. The estimate runs before parsing, so selection is a
deterministic file-level order (non-test files first, shallower paths first, then path); ranking later
orders work inside that scope. Token counts are bounded heuristics from byte and line counts, reported
as a low/high range, never as a quote; the build cap and per-call reservations are what bound spend.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.engine.catalog import Catalog, Preset, cost_micro_usd

CHUNK_LINES = 450  # target 400-500 lines per explanation chunk (07)
PROMPT_OVERHEAD_TOKENS = 1500  # system text, schema, repo map excerpt per call
BYTES_PER_TOKEN_LOW, BYTES_PER_TOKEN_HIGH = 4, 3
EXPLAIN_OUTPUT_LOW, EXPLAIN_OUTPUT_HIGH = 300, 2500
SUMMARY_TOKENS = 150
GUIDE_EVIDENCE_TOKENS = 6000
VALIDITY_SECONDS = 30 * 60

_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/|(_test|\.test|\.spec|_spec)\.[^/]+$|(^|/)test_[^/]+$"
)
Glob = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\\]+$")]


class GenerationConfig(BaseModel):
    """Build configuration (06 "Create estimate request"). Normalized before hashing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    include: Annotated[list[Glob], Field(max_length=50)] = ["**"]
    exclude: Annotated[list[Glob], Field(max_length=50)] = []
    languages: Annotated[list[str], Field(min_length=1, max_length=1)] = ["en"]
    levels: Annotated[list[str], Field(min_length=1, max_length=1)] = ["standard"]
    max_files: Annotated[int, Field(ge=1, le=500)] = 100
    guide_pages: Annotated[int, Field(ge=1, le=8)] = 5
    model_preset: Annotated[str, Field(min_length=1, max_length=48)]

    @field_validator("include", "exclude")
    @classmethod
    def _relative(cls, globs: list[str]) -> list[str]:
        for glob in globs:
            if glob.startswith("/") or ".." in PurePosixPath(glob).parts:
                raise ValueError("globs are relative to the repository root and cannot contain '..'")
        return sorted(set(globs))

    @field_validator("languages")
    @classmethod
    def _languages(cls, values: list[str]) -> list[str]:
        if values != ["en"]:
            raise ValueError("only English output is available (other languages are planned, FR-026)")
        return values

    @field_validator("levels")
    @classmethod
    def _levels(cls, values: list[str]) -> list[str]:
        if values != ["standard"]:
            raise ValueError("only the standard reader level is available")
        return values

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


@dataclass(frozen=True)
class ScannedFile:
    id: str
    path: str
    status: str
    byte_count: int
    line_count: int


def _matches(path: str, globs: list[str]) -> bool:
    pure = PurePosixPath(path)
    return any(pure.full_match(glob) for glob in globs)


def select_files(files: list[ScannedFile], config: GenerationConfig) -> tuple[list[ScannedFile], int]:
    """Returns (selected in deterministic order, number of selectable files left out by scope)."""
    eligible = [
        f
        for f in files
        if f.status == "selected"
        and _matches(f.path, config.include)
        and not _matches(f.path, config.exclude)
    ]
    eligible.sort(key=lambda f: (bool(_TEST_PATH.search(f.path)), f.path.count("/"), f.path))
    return eligible[: config.max_files], len(files) - min(len(eligible), config.max_files)


def config_digest(
    config: GenerationConfig, *, commit_sha: str, revision: str, engine: str, prompts: str
) -> str:
    body = {
        "config": config.canonical(),
        "commit": commit_sha,
        "catalog": revision,
        "engine": engine,
        "prompts": prompts,
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Estimate:
    selected: list[ScannedFile]
    excluded: int
    tokens: dict[str, dict[str, int]]
    low_micro_usd: int
    high_micro_usd: int


def estimate(
    files: list[ScannedFile], config: GenerationConfig, catalog: Catalog, preset: Preset
) -> Estimate:
    selected, excluded = select_files(files, config)
    chunks = sum(max(1, -(-f.line_count // CHUNK_LINES)) for f in selected)
    source_bytes = sum(f.byte_count for f in selected)
    pages = min(config.guide_pages, max(1, len(selected)))

    def bound(bytes_per_token: int, explain_out: int, with_repair: bool) -> dict[str, dict[str, int]]:
        explain_in = -(-source_bytes // bytes_per_token) + chunks * PROMPT_OVERHEAD_TOKENS
        summaries = len(selected) * SUMMARY_TOKENS + PROMPT_OVERHEAD_TOKENS
        writer_in = explain_in + 2 * summaries + pages * (GUIDE_EVIDENCE_TOKENS + PROMPT_OVERHEAD_TOKENS)
        writer_out = (
            chunks * explain_out
            + 1500
            + 1000
            + pages * (preset.writer.max_output_tokens if with_repair else 1500)
        )
        checker_in = pages * (GUIDE_EVIDENCE_TOKENS + PROMPT_OVERHEAD_TOKENS) * (2 if with_repair else 1)
        checker_out = (
            pages * (preset.checker.max_output_tokens if with_repair else 800) * (2 if with_repair else 1)
        )
        if with_repair:  # one repair cycle per page (19 P06)
            writer_in += pages * (GUIDE_EVIDENCE_TOKENS + PROMPT_OVERHEAD_TOKENS)
            writer_out += pages * 1000
        return {
            "writer": {"input": writer_in, "output": writer_out},
            "checker": {"input": checker_in, "output": checker_out},
        }

    def priced(tokens: dict[str, dict[str, int]]) -> int:
        writer = catalog.model(preset.writer.model).price
        checker = catalog.model(preset.checker.model).price
        return cost_micro_usd(
            writer, input_tokens=tokens["writer"]["input"], output_tokens=tokens["writer"]["output"]
        ) + cost_micro_usd(
            checker, input_tokens=tokens["checker"]["input"], output_tokens=tokens["checker"]["output"]
        )

    low = bound(BYTES_PER_TOKEN_LOW, EXPLAIN_OUTPUT_LOW, with_repair=False)
    high = bound(
        BYTES_PER_TOKEN_HIGH, min(EXPLAIN_OUTPUT_HIGH, preset.writer.max_output_tokens), with_repair=True
    )
    return Estimate(
        selected=selected,
        excluded=excluded,
        tokens={
            "low": {
                "input": low["writer"]["input"] + low["checker"]["input"],
                "output": low["writer"]["output"] + low["checker"]["output"],
            },
            "high": {
                "input": high["writer"]["input"] + high["checker"]["input"],
                "output": high["writer"]["output"] + high["checker"]["output"],
            },
            "chunks": {"count": chunks},
            "pages": {"count": pages},
        },
        low_micro_usd=priced(low),
        high_micro_usd=priced(high),
    )
