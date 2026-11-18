"""Catalog validation, integer pricing and the preflight scope/estimate (T10; 07, 11, 23)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.engine import catalog
from app.engine.catalog import Catalog, Price, cost_micro_usd
from app.engine.estimate import GenerationConfig, ScannedFile, config_digest, estimate, select_files

PRICED = {
    "revision": "catalog-2026-11-b",
    "models": [
        {
            "id": "test/writer",
            "provider": "anthropic",
            "provider_model": "test-writer",
            "context_tokens": 200000,
            "max_output_tokens": 8192,
            "structured_output": True,
            "data_region": "us",
            "retention": "test fixture",
            "price": {"input_per_mtok_micro_usd": 3_000_000, "output_per_mtok_micro_usd": 15_000_000},
        }
    ],
    "presets": [
        {
            "name": "balanced-v1",
            "description": "Test preset.",
            "writer": {"model": "test/writer", "max_output_tokens": 4096},
            "checker": {"model": "test/writer", "max_output_tokens": 2048},
        }
    ],
}


def write_catalog(tmp_path: Path, data: dict[str, object]) -> str:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data))
    return str(path)


def files(*specs: tuple[str, int]) -> list[ScannedFile]:
    return [ScannedFile(str(i), path, "selected", lines * 40, lines) for i, (path, lines) in enumerate(specs)]


def test_builtin_catalog_has_only_the_free_replay_preset() -> None:
    builtin = catalog.builtin()
    assert [p.name for p in builtin.presets] == ["replay-v2"]
    assert builtin.model("replay/replay-v2").price == Price(
        input_per_mtok_micro_usd=0, output_per_mtok_micro_usd=0
    )


def test_operator_catalog_adds_presets_and_keeps_replay(tmp_path: Path) -> None:
    loaded = catalog.load(write_catalog(tmp_path, PRICED))
    assert loaded.revision == "catalog-2026-11-b"
    assert {p.name for p in loaded.presets} == {"replay-v2", "balanced-v1"}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(revision=catalog.BUILTIN_REVISION),  # must name its own revision
        lambda d: d.update(revision="2026-11"),  # revision format (23)
        lambda d: d["presets"][0]["writer"].update(model="test/missing"),
        lambda d: d["presets"][0]["writer"].update(max_output_tokens=10_000),  # above the model limit
        lambda d: d["models"][0].update(structured_output=False),
        lambda d: d["models"][0]["price"].update(input_per_mtok_micro_usd=1.5),  # integers only
        lambda d: d["models"][0].update(provider="custom-base-url"),  # no user-supplied endpoints
        lambda d: d["presets"][0].update(name="balanced"),  # presets are versioned <name>-v<N>
    ],
)
def test_invalid_operator_catalogs_are_rejected(tmp_path: Path, mutation: object) -> None:
    data = json.loads(json.dumps(PRICED))
    mutation(data)  # type: ignore[operator]
    with pytest.raises((ValidationError, ValueError)):
        catalog.load(write_catalog(tmp_path, data))


def test_cost_is_an_exact_integer_ceiling() -> None:
    price = Price(input_per_mtok_micro_usd=3_000_000, output_per_mtok_micro_usd=15_000_000)
    assert cost_micro_usd(price, input_tokens=1_000_000, output_tokens=0) == 3_000_000
    assert cost_micro_usd(price, input_tokens=1, output_tokens=0) == 3  # 3 micro-USD per token
    assert (
        cost_micro_usd(
            Price(input_per_mtok_micro_usd=1, output_per_mtok_micro_usd=0), input_tokens=1, output_tokens=0
        )
        == 1
    )
    huge = 10**15
    assert cost_micro_usd(price, input_tokens=huge, output_tokens=huge) == huge * 18  # no float rounding
    cached = Price(
        input_per_mtok_micro_usd=3_000_000, output_per_mtok_micro_usd=0, cache_read_per_mtok_micro_usd=300_000
    )
    assert cost_micro_usd(cached, input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000) == 300_000


def test_scope_selection_is_deterministic_and_prefers_source_over_tests() -> None:
    scanned = files(("tests/test_app.py", 10), ("src/deep/x/y.py", 10), ("src/app.py", 10), ("setup.py", 10))
    scanned.append(ScannedFile("9", "README.md", "unsupported", 10, 1))
    scanned.append(ScannedFile("10", ".env", "excluded", 10, 1))
    config = GenerationConfig(model_preset="replay-v2", max_files=3)
    selected, left_out = select_files(scanned, config)
    assert [f.path for f in selected] == ["setup.py", "src/app.py", "src/deep/x/y.py"]
    assert left_out == 3  # the test file over max_files, plus the two non-selectable files
    narrowed = GenerationConfig(model_preset="replay-v2", include=["src/**"], exclude=["**/deep/**"])
    assert [f.path for f in select_files(scanned, narrowed)[0]] == ["src/app.py"]


@pytest.mark.parametrize(
    "bad",
    [
        {"include": ["../etc/**"]},
        {"include": ["/abs/**"]},
        {"languages": ["de"]},
        {"levels": ["expert"]},
        {"max_files": 0},
        {"guide_pages": 9},
        {"surprise": 1},
    ],
)
def test_config_validation(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GenerationConfig.model_validate({"model_preset": "replay-v2", **bad})


def test_config_digest_binds_commit_catalog_and_versions() -> None:
    config = GenerationConfig(model_preset="replay-v2", include=["b/**", "a/**", "a/**"])
    assert config.include == ["a/**", "b/**"]  # normalized before hashing
    base = {"commit_sha": "a" * 40, "revision": "catalog-2026-11-a", "engine": "0.1.0", "prompts": "0.2.0"}
    digest = config_digest(config, **base)
    for key, value in [("commit_sha", "b" * 40), ("revision", "catalog-2026-11-b"), ("prompts", "0.3.0")]:
        assert config_digest(config, **{**base, key: value}) != digest
    assert (
        config_digest(GenerationConfig(model_preset="replay-v2", include=["a/**", "b/**"]), **base) == digest
    )


def test_estimate_range_and_prices(tmp_path: Path) -> None:
    priced: Catalog = catalog.load(write_catalog(tmp_path, PRICED))
    scanned = files(("src/a.py", 100), ("src/b.py", 1000))  # b.py has three 450-line chunks
    config = GenerationConfig(model_preset="balanced-v1", guide_pages=3)
    preset = priced.preset("balanced-v1")
    assert preset is not None
    result = estimate(scanned, config, priced, preset)
    assert result.tokens["chunks"]["count"] == 4 and result.tokens["pages"]["count"] == 2  # pages <= files
    assert 0 < result.low_micro_usd < result.high_micro_usd
    assert estimate(scanned, config, priced, preset) == result  # deterministic
    replay = catalog.builtin()
    replay_preset = replay.preset("replay-v2")
    assert replay_preset is not None
    free = estimate(scanned, config.model_copy(update={"model_preset": "replay-v2"}), replay, replay_preset)
    assert (free.low_micro_usd, free.high_micro_usd) == (0, 0)
