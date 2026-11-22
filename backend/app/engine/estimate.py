"""Generation scope and preflight estimate (07 "estimate/approve", 01 FR-006/FR-007).

`select_files` is the single scope function: the estimate counts exactly the files the build will
explain, so an approved estimate fixes the scope. The estimate runs before parsing, so selection is a
deterministic file-level order (non-test files first, shallower paths first, then path); ranking later
orders work inside that scope. Token counts are bounded heuristics from byte and line counts, reported
as a low/high range, never as a quote; the build cap and per-call reservations are what bound spend.

The high bound counts the most guide pages the plan may write (`generate.page_bounds`), and the low
bound the fewest. Reasoning or thinking tokens bill as output (07), so a role with an `effort` may spend
its whole `max_output_tokens` on any call: the high bound then counts every call of that role at its
maximum, which is what each call earmarks, instead of the typical answer sizes below.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.engine.catalog import Catalog, Preset, Role, cost_micro_usd
from app.engine.generate import page_bounds
from app.engine.globs import Budget
from app.engine.globs import match as globs_match
from app.engine.prompts import LANGUAGES, LEVELS
from app.engine.prompts import variant as variant_label

CHUNK_LINES = 450  # target 400-500 lines per explanation chunk (07)
PROMPT_OVERHEAD_TOKENS = 1500  # system text, schema, repo map excerpt per call
BYTES_PER_TOKEN_LOW, BYTES_PER_TOKEN_HIGH = 4, 3
EXPLAIN_OUTPUT_LOW, EXPLAIN_OUTPUT_HIGH = 300, 2500
OVERVIEW_OUTPUT, PLAN_OUTPUT, REPAIR_OUTPUT = 1500, 1000, 1000
GUIDE_OUTPUT_LOW, CHECK_OUTPUT_LOW = 1500, 800
SUMMARY_TOKENS = 150
GUIDE_EVIDENCE_TOKENS = 6000
VALIDITY_SECONDS = 30 * 60
MAX_GLOB_STARS = 8
MAX_GLOB_SEGMENTS = 16

_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/|(_test|\.test|\.spec|_spec)\.[^/]+$|(^|/)test_[^/]+$"
)
# Printable path globs without character classes or backslashes; matched in bounded time (globs.py).
Glob = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\\\[\]?]+$")]


class GenerationConfig(BaseModel):
    """Build configuration (06 "Create estimate request"). Normalized before hashing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    include: Annotated[list[Glob], Field(max_length=20)] = ["**"]
    exclude: Annotated[list[Glob], Field(max_length=20)] = []
    # One build writes languages[0]; further languages (a family, FR-026) become variant builds of it.
    languages: Annotated[list[str], Field(min_length=1, max_length=3)] = ["en"]
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
            if glob.count("*") > MAX_GLOB_STARS or glob.count("/") >= MAX_GLOB_SEGMENTS:
                raise ValueError(
                    f"globs may have at most {MAX_GLOB_STARS} '*' and {MAX_GLOB_SEGMENTS} segments"
                )
        return sorted(set(globs))

    # A variant (M6, FR-026): the same snapshot, scope and guide plan as a ready base build, written for
    # another language or reader level, so page and source anchors are identical across variants.
    variant_of: Annotated[str | None, Field(pattern=r"^[0-9a-f-]{36}$")] = None
    # Explain files through the provider's batch interface at the catalog batch price (M6, T24).
    batch: bool = False
    # Language variants translate their base build's validated text (P07, prompt sets from 0.6.0) instead
    # of writing from the source: cheaper, and every language says the same. Reader level is unchanged.
    translate: bool = False

    @field_validator("languages")
    @classmethod
    def _languages(cls, values: list[str]) -> list[str]:
        if any(v not in LANGUAGES for v in values):
            raise ValueError(f"languages are {', '.join(LANGUAGES)}")
        if len(set(values)) != len(values):
            raise ValueError("each language may be named once")
        return values

    @model_validator(mode="after")
    def _one_language_per_variant(self) -> GenerationConfig:
        if self.variant_of is not None and len(self.languages) != 1:
            raise ValueError("a variant build writes one language")
        if self.translate and self.variant_of is None and len(self.languages) < 2:
            raise ValueError("translation needs a further language or a base build")
        return self

    @property
    def family(self) -> list[str]:
        """The languages this config builds: its own first, then one variant build per further language."""
        return list(self.languages)

    def for_language(self, language: str, base_id: str) -> GenerationConfig:
        """The config of the variant build that writes `language` for the base build `base_id`."""
        return self.model_copy(update={"languages": [language], "variant_of": base_id})

    @field_validator("levels")
    @classmethod
    def _levels(cls, values: list[str]) -> list[str]:
        if any(v not in LEVELS for v in values):
            raise ValueError(f"reader levels are {', '.join(LEVELS)}")
        return values

    @property
    def variant(self) -> str:
        return variant_label(self.languages[0], self.levels[0])

    def canonical(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        if data["variant_of"] is None:
            del data["variant_of"]  # configs approved before variants keep their digests
        if not data["batch"]:
            del data["batch"]
        if not data["translate"]:
            del data["translate"]
        return data


@dataclass(frozen=True)
class ScannedFile:
    id: str
    path: str
    status: str
    byte_count: int
    line_count: int


def _matches(path: str, globs: list[str], budget: Budget) -> bool:
    return any(globs_match(path, glob, budget) for glob in globs)


def select_files(files: list[ScannedFile], config: GenerationConfig) -> tuple[list[ScannedFile], int]:
    """Returns (selected in deterministic order, number of selectable files left out by scope).
    Raises GlobBudgetExceeded when the globs are too expensive for these paths."""
    budget = Budget()
    eligible = [
        f
        for f in files
        if f.status == "selected"
        and _matches(f.path, config.include, budget)
        and not _matches(f.path, config.exclude, budget)
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
    # One language variant translated from the base (P07): no plan, the base's output as input.
    translated_low_micro_usd: int = 0
    translated_high_micro_usd: int = 0


def estimate(
    files: list[ScannedFile], config: GenerationConfig, catalog: Catalog, preset: Preset
) -> Estimate:
    selected, excluded = select_files(files, config)
    chunks = sum(max(1, -(-f.line_count // CHUNK_LINES)) for f in selected)
    source_bytes = sum(f.byte_count for f in selected)
    fewest, most = page_bounds(len(selected), config.guide_pages)

    def high_output(role: Role, typical: int) -> int:
        """What one call of `role` may bill as output in the high bound (module docstring)."""
        return role.max_output_tokens if role.effort is not None else min(typical, role.max_output_tokens)

    def bound(bytes_per_token: int, high: bool) -> dict[str, dict[str, int]]:
        writer, checker = preset.writer, preset.checker
        pages = most if high else fewest
        explain_in = -(-source_bytes // bytes_per_token) + chunks * PROMPT_OVERHEAD_TOKENS
        summaries = len(selected) * SUMMARY_TOKENS + PROMPT_OVERHEAD_TOKENS
        writer_in = explain_in + 2 * summaries + pages * (GUIDE_EVIDENCE_TOKENS + PROMPT_OVERHEAD_TOKENS)
        checker_in = pages * (GUIDE_EVIDENCE_TOKENS + PROMPT_OVERHEAD_TOKENS)
        if not high:
            writer_out = (
                chunks * EXPLAIN_OUTPUT_LOW + OVERVIEW_OUTPUT + PLAN_OUTPUT + pages * GUIDE_OUTPUT_LOW
            )
            checker_out = pages * CHECK_OUTPUT_LOW
        else:
            writer_out = (
                chunks * high_output(writer, EXPLAIN_OUTPUT_HIGH)
                + high_output(writer, OVERVIEW_OUTPUT)
                + high_output(writer, PLAN_OUTPUT)
                + pages * writer.max_output_tokens
            )
            # One repair cycle per page (19 P06): the writer's repair, then the changed claims checked again.
            writer_in += pages * (GUIDE_EVIDENCE_TOKENS + PROMPT_OVERHEAD_TOKENS)
            writer_out += pages * high_output(writer, REPAIR_OUTPUT)
            checker_in *= 2
            checker_out = pages * checker.max_output_tokens * 2
        return {
            "writer": {"input": writer_in, "output": writer_out},
            "checker": {"input": checker_in, "output": checker_out},
        }

    def translated(bytes_per_token: int, high: bool) -> dict[str, dict[str, int]]:
        """A translated variant reads the base's answers instead of the source (19 P07) and writes as much;
        the checker still checks every page, and a page may still need its repair cycle."""
        writer, checker = preset.writer, preset.checker
        pages = most if high else fewest
        explain_out = high_output(writer, EXPLAIN_OUTPUT_HIGH) if high else EXPLAIN_OUTPUT_LOW
        overview_out = high_output(writer, OVERVIEW_OUTPUT) if high else OVERVIEW_OUTPUT
        page_out = writer.max_output_tokens if high else GUIDE_OUTPUT_LOW
        writer_in = (
            chunks * (EXPLAIN_OUTPUT_HIGH if high else EXPLAIN_OUTPUT_LOW)
            + chunks * PROMPT_OVERHEAD_TOKENS
            + OVERVIEW_OUTPUT
            + PROMPT_OVERHEAD_TOKENS
            + pages * (page_out + PROMPT_OVERHEAD_TOKENS)
        )
        writer_out = chunks * explain_out + overview_out + pages * page_out
        checker_in = pages * (GUIDE_EVIDENCE_TOKENS + PROMPT_OVERHEAD_TOKENS)
        checker_out = pages * CHECK_OUTPUT_LOW
        if high:
            writer_in += pages * (GUIDE_EVIDENCE_TOKENS + PROMPT_OVERHEAD_TOKENS)
            writer_out += pages * high_output(writer, REPAIR_OUTPUT)
            checker_in *= 2
            checker_out = pages * checker.max_output_tokens * 2
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

    def batch_saving(bytes_per_token: int, explain_out: int) -> int:
        """What batching the explain requests saves at most. Only the low bound uses it: answers a batch
        cannot give are made synchronously at the standard price, so the high bound stays standard."""
        writer = catalog.model(preset.writer.model)
        if not config.batch or writer.batch_price is None:
            return 0
        explain_in = -(-source_bytes // bytes_per_token) + chunks * PROMPT_OVERHEAD_TOKENS
        explain_out_tokens = chunks * explain_out
        standard = cost_micro_usd(writer.price, input_tokens=explain_in, output_tokens=explain_out_tokens)
        batched = cost_micro_usd(
            writer.batch_price, input_tokens=explain_in, output_tokens=explain_out_tokens
        )
        return max(0, standard - batched)

    low = bound(BYTES_PER_TOKEN_LOW, high=False)
    high = bound(BYTES_PER_TOKEN_HIGH, high=True)
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
            "pages": {"count": most},  # the most the plan may write
        },
        low_micro_usd=max(0, priced(low) - batch_saving(BYTES_PER_TOKEN_LOW, EXPLAIN_OUTPUT_LOW)),
        high_micro_usd=priced(high),
        translated_low_micro_usd=priced(translated(BYTES_PER_TOKEN_LOW, high=False)),
        translated_high_micro_usd=priced(translated(BYTES_PER_TOKEN_HIGH, high=True)),
    )
