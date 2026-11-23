"""TypeScript path aliases for scip-typescript (app.engine.ts_project, T23)."""

from __future__ import annotations

from app.engine import ts_project
from app.engine.ts_project import parse_jsonc, projects

SRC = "/w/src"


def test_tsconfig_comments_and_trailing_commas_parse() -> None:
    text = """{
      // a comment with "quotes"
      "compilerOptions": { /* block */ "baseUrl": ".", "paths": { "@/*": ["./src/*",], }, },
      "x": "not // a comment, nor /* this */",
    }"""
    data = parse_jsonc(text)
    assert data["compilerOptions"]["paths"] == {"@/*": ["./src/*"]}
    assert data["x"] == "not // a comment, nor /* this */"
    assert parse_jsonc('{"a": "esc\\"aped // still string"}') == {"a": 'esc"aped // still string'}
    assert parse_jsonc("{nope") is None


def test_no_aliases_means_no_projects() -> None:
    assert projects({}, ["a.ts"], SRC) == []
    assert projects({"tsconfig.json": '{"compilerOptions": {"strict": true}}'}, ["a.ts"], SRC) == []


def test_paths_resolve_relative_to_base_url_or_the_config() -> None:
    configs = {
        "tsconfig.json": '{"compilerOptions": {"baseUrl": "src", "paths": {"@/*": ["*"], "~lib": ["lib/index.ts"]}}}',
        "tools/tsconfig.json": '{"compilerOptions": {"paths": {"#t/*": ["./helpers/*"]}}}',
    }
    result = projects(configs, ["src/a.ts", "src/lib/index.ts", "tools/run.ts", "tools/helpers/x.ts"], SRC)
    assert [r["files"] for r in result] == [
        ["/w/src/src/a.ts", "/w/src/src/lib/index.ts"],
        ["/w/src/tools/helpers/x.ts", "/w/src/tools/run.ts"],
    ]
    root, tools = (r["compilerOptions"] for r in result)
    assert root["baseUrl"] == "/w/src/src"
    assert root["paths"] == {"@/*": ["/w/src/src/*"], "~lib": ["/w/src/src/lib/index.ts"]}
    assert "baseUrl" not in tools and tools["paths"] == {"#t/*": ["/w/src/tools/helpers/*"]}
    assert root["types"] == [] and root["maxNodeModuleJsDepth"] == 0  # the indexer's own options


def test_each_file_belongs_to_its_nearest_config_and_the_rest_to_a_plain_project() -> None:
    configs = {
        "apps/web/tsconfig.json": '{"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}',
        "apps/admin/tsconfig.json": '{"compilerOptions": {"paths": {"@/*": ["./app/*"]}}}',
        "packages/ui/tsconfig.json": '{"compilerOptions": {"strict": true}}',  # no aliases
    }
    files = ["apps/web/src/x.ts", "apps/admin/app/x.ts", "packages/ui/button.tsx", "scripts/build.js"]
    result = projects(configs, files, SRC)
    assert [(r["files"], r["compilerOptions"].get("paths")) for r in result] == [
        (["/w/src/apps/admin/app/x.ts"], {"@/*": ["/w/src/apps/admin/app/*"]}),
        (["/w/src/apps/web/src/x.ts"], {"@/*": ["/w/src/apps/web/src/*"]}),
        (["/w/src/packages/ui/button.tsx", "/w/src/scripts/build.js"], None),
    ]


def test_extends_inside_the_snapshot_is_followed_and_packages_are_skipped() -> None:
    configs = {
        "tsconfig.base.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"@shared/*": ["packages/shared/*"]}}}',
        "apps/web/tsconfig.json": '{"extends": ["@tsconfig/node20/tsconfig.json", "../../tsconfig.base"]}',
        "apps/api/tsconfig.json": (
            '{"extends": "../../tsconfig.base.json", "compilerOptions": {"paths": {"@/*": ["./src/*"]}}}'
        ),
    }
    result = projects(configs, ["apps/web/a.ts", "apps/api/src/b.ts"], SRC)
    api, web = (r["compilerOptions"] for r in result)
    # The base's baseUrl stays relative to the base; the child's own paths replace the base's.
    assert web["baseUrl"] == "/w/src" and web["paths"] == {"@shared/*": ["/w/src/packages/shared/*"]}
    assert api["baseUrl"] == "/w/src" and api["paths"] == {"@/*": ["/w/src/src/*"]}


def test_extends_cycles_end() -> None:
    configs = {
        "tsconfig.json": '{"extends": "./tsconfig.a.json", "compilerOptions": {"paths": {"@/*": ["./*"]}}}',
        "tsconfig.a.json": '{"extends": "./tsconfig.json"}',
    }
    assert projects(configs, ["a.ts"], SRC)[0]["compilerOptions"]["paths"] == {"@/*": ["/w/src/*"]}


def test_targets_never_leave_the_checkout_and_bad_patterns_are_dropped() -> None:
    configs = {
        "tsconfig.json": """{"compilerOptions": {"baseUrl": "..", "paths": {
            "a/*": ["../../etc/*", "/abs/*", "ok/*"], "b/**": ["x/*/*"], "c": "not a list", "d": [3]
        }}}"""
    }
    result = projects(configs, ["ok/a.ts"], SRC)
    options = result[0]["compilerOptions"]
    assert "baseUrl" not in options  # ".." leaves the checkout
    assert options["paths"] == {"a/*": ["/w/src/ok/*"]}


def test_limits() -> None:
    big = (
        '{"compilerOptions": {"paths": {"@/*": ["./*"]}}, "pad": "' + "x" * ts_project.MAX_CONFIG_BYTES + '"}'
    )
    assert projects({"tsconfig.json": big}, ["a.ts"], SRC) == []
    many = ", ".join(f'"a{n}/*": ["./{n}/*"]' for n in range(ts_project.MAX_ALIASES + 50))
    result = projects({"tsconfig.json": '{"compilerOptions": {"paths": {' + many + "}}}"}, ["a.ts"], SRC)
    assert len(result[0]["compilerOptions"]["paths"]) == ts_project.MAX_ALIASES
    assert ts_project.is_config("a/tsconfig.app.json") and ts_project.is_config("jsconfig.json")
    assert not ts_project.is_config("a/config.json") and not ts_project.is_config("tsconfig.json.bak")


def test_a_solution_config_hands_files_to_the_configs_it_references() -> None:
    """Vite's layout: tsconfig.json owns nothing and references the app and node configs, where the alias is."""
    configs = {
        "tsconfig.json": '{"files": [], "references": [{"path": "./tsconfig.app.json"}, {"path": "./tsconfig.node.json"}]}',
        "tsconfig.app.json": '{"include": ["src"], "exclude": ["src/**/*.test.ts"],'
        ' "compilerOptions": {"paths": {"@/*": ["./src/*"]}}}',
        "tsconfig.node.json": '{"include": ["vite.config.ts"]}',
    }
    files = ["src/main.tsx", "src/lib/utils.ts", "src/lib/utils.test.ts", "vite.config.ts"]
    result = projects(configs, files, SRC)
    assert [(r["files"], r["compilerOptions"].get("paths")) for r in result] == [
        (["/w/src/src/lib/utils.ts", "/w/src/src/main.tsx"], {"@/*": ["/w/src/src/*"]}),
        (["/w/src/src/lib/utils.test.ts", "/w/src/vite.config.ts"], None),
    ]


def test_a_referenced_config_of_any_name_is_a_project_for_its_directory() -> None:
    configs = {
        "tsconfig.json": '{"files": [], "references": [{"path": "packages/core/tsconfig.lib.json"}, {"path": "nope"}]}',
        "packages/core/tsconfig.lib.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"#c/*": ["lib/*"]}}}',
    }
    result = projects(configs, ["packages/core/lib/a.ts", "tools/x.ts"], SRC)
    assert [r["files"] for r in result] == [["/w/src/packages/core/lib/a.ts"], ["/w/src/tools/x.ts"]]
    assert result[0]["compilerOptions"]["paths"] == {"#c/*": ["/w/src/packages/core/lib/*"]}


def test_workspace_packages_resolve_by_name_to_their_sources() -> None:
    packages = {
        "package.json": '{"name": "monorepo", "private": true, "workspaces": ["packages/*"]}',
        "packages/ui/package.json": '{"name": "@acme/ui", "main": "dist/index.js", "types": "dist/index.d.ts"}',
        "packages/core/package.json": '{"name": "@acme/core", "exports": {".": {"import": "./src/main.ts"}}}',
        "packages/bad/package.json": '{"name": "Not A Name", "main": "index.ts"}',
        "packages/web/package.json": '{"name": "web"}',
    }
    files = [
        "packages/ui/src/index.ts",
        "packages/ui/src/button.tsx",
        "packages/core/src/main.ts",
        "packages/bad/index.ts",
        "packages/web/app.ts",
    ]
    result = projects({}, files, SRC, packages)
    assert len(result) == 1  # no tsconfig anywhere: one project, with the packages' names
    assert result[0]["compilerOptions"]["paths"] == {
        "@acme/core": ["/w/src/packages/core/src/main.ts"],
        "@acme/core/*": ["/w/src/packages/core/src/*"],
        "@acme/ui": ["/w/src/packages/ui/src/index.ts"],  # dist/index.js mapped to its source
        "@acme/ui/*": ["/w/src/packages/ui/src/*"],
        "web/*": ["/w/src/packages/web/*"],  # no entry in the checkout: only its files by path
    }
    # A project's own paths win over a package of the same name.
    own = {"tsconfig.json": '{"compilerOptions": {"paths": {"@acme/ui": ["./shim.ts"]}}}'}
    paths = projects(own, ["shim.ts", *files], SRC, packages)[0]["compilerOptions"]["paths"]
    assert paths["@acme/ui"] == ["/w/src/shim.ts"] and paths["@acme/core"] == [
        "/w/src/packages/core/src/main.ts"
    ]


def test_root_dirs_and_module_suffixes_pass_through_inside_the_checkout() -> None:
    configs = {
        "tsconfig.json": '{"compilerOptions": {"rootDirs": ["src", "generated", "../../etc"],'
        ' "moduleSuffixes": [".ios", "", "../x", 5]}}'
    }
    options = projects(configs, ["src/a.ts"], SRC)[0]["compilerOptions"]
    assert options["rootDirs"] == ["/w/src/src", "/w/src/generated"]
    assert options["moduleSuffixes"] == [".ios", ""]
    assert "paths" not in options


def test_include_patterns() -> None:
    glob = ts_project._glob
    assert glob("", "src").fullmatch("src/a/b.ts") and not glob("", "src").fullmatch("srcx/a.ts")
    assert glob("app", "**/*.ts").fullmatch("app/x/y.ts") and glob("app", "**/*.ts").fullmatch("app/y.ts")
    assert not glob("app", "*.ts").fullmatch("app/x/y.ts")
    assert glob("", "../outside") is None


def test_globs_match_in_linear_time() -> None:
    """Security review: a repository's patterns never make matching backtrack."""
    import time

    glob = ts_project._glob
    hostile = glob("", "/".join(["**"] * 40) + "/" + "*a" * 40 + "b")
    path = "/".join(["x"] * 60) + "/" + "a" * 200
    started = time.monotonic()
    assert hostile is not None and not hostile.fullmatch(path)
    assert time.monotonic() - started < 1.0
    assert glob("", "src/**/*.test.ts").fullmatch("src/a/b/c.test.ts")
    assert glob("", "src/**/*.test.ts").fullmatch("src/c.test.ts")
    assert not glob("", "src/**/*.test.ts").fullmatch("src/c.ts")
    assert glob("", "a?c.ts").fullmatch("abc.ts") and not glob("", "a?c.ts").fullmatch("ac.ts")
