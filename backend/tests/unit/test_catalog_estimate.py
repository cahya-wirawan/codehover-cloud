"""Catalog validation, integer pricing and the preflight scope/estimate (T10; 07, 11, 23)."""

from __future__ import annotations

import contextlib
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
        lambda d: d["presets"][0]["checker"].update(
            model="replay/replay-v2"
        ),  # heuristic checker on model text
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
        {"languages": ["fr"]},  # FR-026 offers en, de and id
        {"levels": ["casual"]},
        {"languages": ["de", "id"]},  # one variant per build
        {"variant_of": "not-a-build-id"},
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
    # Two files may still get three pages: the plan writes 1-2 pages for tiny scopes, up to guide_pages.
    assert result.tokens["chunks"]["count"] == 4 and result.tokens["pages"]["count"] == 3
    assert 0 < result.low_micro_usd < result.high_micro_usd
    assert estimate(scanned, config, priced, preset) == result  # deterministic
    replay = catalog.builtin()
    replay_preset = replay.preset("replay-v2")
    assert replay_preset is not None
    free = estimate(scanned, config.model_copy(update={"model_preset": "replay-v2"}), replay, replay_preset)
    assert (free.low_micro_usd, free.high_micro_usd) == (0, 0)


def test_the_high_bound_counts_the_most_pages_and_reasoning_at_its_maximum(tmp_path: Path) -> None:
    """A one-file build may still get five pages, and a role with an effort may bill its whole
    max_output_tokens as reasoning on every call; the high bound counts both (07 "estimate")."""
    effort = json.loads(json.dumps(PRICED))
    effort["presets"].append(
        {
            "name": "reasoning-v1",
            "description": "Test preset with an effort.",
            "writer": {"model": "test/writer", "max_output_tokens": 4096, "effort": "medium"},
            "checker": {"model": "test/writer", "max_output_tokens": 2048, "effort": "medium"},
        }
    )
    priced: Catalog = catalog.load(write_catalog(tmp_path, effort))
    one = files(("src/a.py", 100))
    config = GenerationConfig(model_preset="balanced-v1", guide_pages=5)
    plain = estimate(one, config, priced, priced.preset("balanced-v1"))  # type: ignore[arg-type]
    reasoning = estimate(
        one,
        config.model_copy(update={"model_preset": "reasoning-v1"}),
        priced,
        priced.preset("reasoning-v1"),  # type: ignore[arg-type]
    )
    assert plain.tokens["pages"]["count"] == reasoning.tokens["pages"]["count"] == 5
    pages, writer_max, checker_max = 5, 4096, 2048
    # explain 1 chunk + overview + plan + 5 guide pages + 5 repairs, every call at the writer's maximum,
    # and an initial check plus a recheck per page at the checker's maximum.
    assert reasoning.tokens["high"]["output"] == (3 + 2 * pages) * writer_max + 2 * pages * checker_max
    # Without an effort only the guide pages and checks are counted at their maximum.
    assert (
        plain.tokens["high"]["output"]
        == 2500 + 1500 + 1000 + pages * (writer_max + 1000) + 2 * pages * checker_max
    )
    assert plain.low_micro_usd == reasoning.low_micro_usd < plain.high_micro_usd < reasoning.high_micro_usd
    assert plain.tokens["low"]["output"] == 300 + 1500 + 1000 + 1 * 1500 + 1 * 800  # the fewest pages: one


GLOB_PATHS = [
    "a.py",
    "src/a.py",
    "src/x/y/b.ts",
    "tests/test_a.py",
    "x/fixtures/a.py",
    "fixtures/a.py",
    "a/b",
    ".env",
]
GLOB_PATTERNS = [
    "**",
    "*",
    "src/**",
    "**/*.py",
    "src/*.py",
    "**/fixtures/**",
    "*/*",
    "src/**/b.ts",
    "a*.py",
    "*a*b*",
    "a/**/b",
    "**/**",
]


def test_glob_matcher_agrees_with_pathlib_on_ordinary_globs() -> None:
    from pathlib import PurePosixPath

    from app.engine.globs import match

    for path in GLOB_PATHS:
        for glob in GLOB_PATTERNS:
            assert match(path, glob) == PurePosixPath(path).full_match(glob), (path, glob)


def test_glob_matching_is_bounded_on_hostile_globs() -> None:
    """pathlib's regex translation takes exponential time on these; a selection must stay within its budget
    and stop with GlobBudgetExceeded rather than run unbounded (paths and globs are tenant-controlled)."""
    import time

    from app.engine.globs import GlobBudgetExceeded

    name = "a" * 200
    scanned = [ScannedFile(str(i), "/".join([name] * 29) + f"/{i}", "selected", 1, 1) for i in range(2000)]
    for glob in ["**/*a*a*a*b/**", "**/**/**/**/b", "*a*a*a*a*a*a*a*b"]:
        config = GenerationConfig(
            model_preset="replay-v2",
            include=[f"{glob}{i}" for i in range(20)],
            exclude=[f"{glob}x{i}" for i in range(20)],
        )
        started = time.perf_counter()
        with contextlib.suppress(GlobBudgetExceeded):
            select_files(scanned, config)
        assert time.perf_counter() - started < 5.0, glob
    ordinary = GenerationConfig(
        model_preset="replay-v2", include=["src/**", "**/*.py"], exclude=["**/tests/**"]
    )
    select_files(scanned, ordinary)  # deep, long paths with ordinary globs stay within budget


@pytest.mark.parametrize("glob", ["src/[a-z]*.py", "src/?.py", "*a*a*a*a*a*a*a*a*a*b", "/".join(["d"] * 17)])
def test_unsupported_or_oversized_globs_are_rejected(glob: str) -> None:
    with pytest.raises(ValidationError):
        GenerationConfig(model_preset="replay-v2", include=[glob])


def test_example_operator_catalog_is_valid() -> None:
    example = Path(__file__).resolve().parents[3] / "deployment" / "model-catalog.example.json"
    loaded = catalog.load(str(example))
    assert loaded.revision != catalog.BUILTIN_REVISION
    preset = loaded.preset("balanced-v1")
    assert preset is not None and loaded.model(preset.writer.model).provider == "anthropic"
    assert all(m.price.input_per_mtok_micro_usd > 0 for m in loaded.models if m.provider != "replay")


def test_variant_configs_keep_old_digests_stable() -> None:
    plain = GenerationConfig(model_preset="replay-v2")
    assert "variant_of" not in plain.canonical() and plain.variant == "en-standard"
    german = GenerationConfig(model_preset="replay-v2", languages=["de"], levels=["beginner"])
    assert german.variant == "de-beginner"
    base = "0" * 8 + "-0000-7000-8000-" + "0" * 12
    variant = GenerationConfig(model_preset="replay-v2", languages=["de"], variant_of=base)
    assert variant.canonical()["variant_of"] == base
