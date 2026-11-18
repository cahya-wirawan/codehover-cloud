"""Approved model catalog and presets (07 "Provider abstraction", 23 presets and price revisions).

The catalog binds each model to its provider, context and output limits, structured-output support,
approved data handling, and an integer price per million tokens. A preset (`<name>-v<N>`) binds the
pipeline roles (writer, checker) to catalog models and fixes their parameters. Both are immutable once
published: a price change is a new revision `catalog-YYYY-MM-<letter>`, a model or parameter change a new
preset version.

There are no built-in guessed model names or prices. The built-in catalog has only the deterministic
replay model (zero cost); operators add provider models through `MODEL_CATALOG_FILE`, which must name a
new revision whenever any price changes.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BUILTIN_REVISION = "catalog-2026-11-a"
MILLION = 1_000_000

Revision = Annotated[str, Field(pattern=r"^catalog-[0-9]{4}-[0-9]{2}-[a-z]$")]
PresetName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,40}-v[1-9][0-9]{0,3}$")]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Price(_Frozen):
    """Integer USD micro-units per million tokens; cache prices default to the input price."""

    input_per_mtok_micro_usd: Annotated[int, Field(ge=0)]
    output_per_mtok_micro_usd: Annotated[int, Field(ge=0)]
    cache_read_per_mtok_micro_usd: Annotated[int | None, Field(ge=0)] = None
    cache_write_per_mtok_micro_usd: Annotated[int | None, Field(ge=0)] = None


class Model(_Frozen):
    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    provider: Literal["replay", "anthropic"]
    provider_model: Annotated[str, Field(min_length=1, max_length=128)]
    context_tokens: Annotated[int, Field(ge=1024)]
    max_output_tokens: Annotated[int, Field(ge=256)]
    structured_output: bool
    data_region: Annotated[str, Field(min_length=1, max_length=40)]
    retention: Annotated[str, Field(min_length=1, max_length=200)]
    price: Price


class Role(_Frozen):
    """A pipeline role bound to a catalog model. No sampling parameters: current provider API versions
    reject them; `effort` is the provider's depth control where supported."""

    model: str
    max_output_tokens: Annotated[int, Field(ge=256)]
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


class Preset(_Frozen):
    name: PresetName
    description: Annotated[str, Field(min_length=1, max_length=200)]
    writer: Role
    checker: Role


class Catalog(_Frozen):
    revision: Revision
    models: list[Model]
    presets: list[Preset]

    @model_validator(mode="after")
    def _consistent(self) -> Catalog:
        models = {m.id: m for m in self.models}
        if len(models) != len(self.models):
            raise ValueError("duplicate model id")
        if len({p.name for p in self.presets}) != len(self.presets):
            raise ValueError("duplicate preset name")
        for preset in self.presets:
            for role in (preset.writer, preset.checker):
                model = models.get(role.model)
                if model is None:
                    raise ValueError(f"preset {preset.name} names unknown model {role.model}")
                if role.max_output_tokens > model.max_output_tokens:
                    raise ValueError(f"preset {preset.name} exceeds the output limit of {model.id}")
                if not model.structured_output:
                    raise ValueError(f"{model.id} lacks structured output, required by every role")
            writer, checker = models[preset.writer.model], models[preset.checker.model]
            if checker.provider == "replay" and writer.provider != "replay":
                # The replay checker matches identifiers; the eval baseline shows it labels unsupported
                # claims supported, so it may only check its own deterministic replay pages.
                raise ValueError(f"preset {preset.name} pairs a model writer with the replay checker")
        return self

    def model(self, model_id: str) -> Model:
        return next(m for m in self.models if m.id == model_id)

    def preset(self, name: str) -> Preset | None:
        return next((p for p in self.presets if p.name == name), None)


REPLAY_MODEL = Model(
    id="replay/replay-v2",
    provider="replay",
    provider_model="replay-v2",
    context_tokens=200_000,
    max_output_tokens=8192,
    structured_output=True,
    data_region="local",
    retention="none: deterministic, runs inside the worker",
    price=Price(input_per_mtok_micro_usd=0, output_per_mtok_micro_usd=0),
)
REPLAY_PRESET = Preset(
    name="replay-v2",
    description="Deterministic replay of the full pipeline from source evidence; no model, no cost.",
    writer=Role(model=REPLAY_MODEL.id, max_output_tokens=4096),
    checker=Role(model=REPLAY_MODEL.id, max_output_tokens=2048),
)


def builtin() -> Catalog:
    return Catalog(revision=BUILTIN_REVISION, models=[REPLAY_MODEL], presets=[REPLAY_PRESET])


def load(path: str | None) -> Catalog:
    """The operator file replaces the revision and adds models/presets; the replay preset stays available."""
    if not path:
        return builtin()
    data = json.loads(Path(path).read_text())
    operator = Catalog.model_validate(data)
    if operator.revision == BUILTIN_REVISION:
        raise ValueError("an operator catalog must use its own revision")
    return Catalog(
        revision=operator.revision,
        models=[REPLAY_MODEL, *[m for m in operator.models if m.id != REPLAY_MODEL.id]],
        presets=[REPLAY_PRESET, *[p for p in operator.presets if p.name != REPLAY_PRESET.name]],
    )


@lru_cache(maxsize=4)
def cached(path: str | None) -> Catalog:
    return load(path)


def cost_micro_usd(
    price: Price,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> int:
    """Integer cost, rounded up so a reservation computed from token maxima is never below the charge."""
    read = price.cache_read_per_mtok_micro_usd
    write = price.cache_write_per_mtok_micro_usd
    total = (
        input_tokens * price.input_per_mtok_micro_usd
        + output_tokens * price.output_per_mtok_micro_usd
        + cache_read_tokens * (price.input_per_mtok_micro_usd if read is None else read)
        + cache_write_tokens * (price.input_per_mtok_micro_usd if write is None else write)
    )
    return -(-total // MILLION)  # exact integer ceiling
