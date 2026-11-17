#!/usr/bin/env python3
"""Application version consistency (build-package/23_VERSIONING_AND_RELEASES.md).

VERSION is the source of truth; every other place that states the application version must agree.
SPEC_VERSION names the build-package release this implementation conforms to: it must be a released
version in build-package/CHANGELOG.md and not newer than build-package/VERSION. The spec may move
ahead of the implementation; that is reported, not failed.
With --tag vX.Y.Z (release CI), the tag must equal v + VERSION. Standard library only.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


def stated_versions() -> dict[str, str]:
    init = (ROOT / "backend/app/__init__.py").read_text()
    match = re.search(r'^__version__ = "([^"]+)"$', init, re.M)
    lock = json.loads((ROOT / "frontend/package-lock.json").read_text())
    return {
        "backend/pyproject.toml": tomllib.loads((ROOT / "backend/pyproject.toml").read_text())["project"]["version"],
        "backend/app/__init__.py": match.group(1) if match else "<missing>",
        "frontend/package.json": json.loads((ROOT / "frontend/package.json").read_text())["version"],
        "frontend/package-lock.json": lock["packages"][""]["version"],
        "contracts/openapi.json": json.loads((ROOT / "contracts/openapi.json").read_text())["info"]["version"],
    }


RELEASE_HEADING = re.compile(r"^## \[([^\]]+)\] - \d{4}-\d{2}-\d{2}[ \t]*$", re.M)


def precedence_key(text: str) -> tuple[int, int, int, int, tuple[tuple[int, int, str], ...]]:
    """SemVer precedence; build metadata ignored."""
    match = SEMVER.match(text)
    if not match:
        raise ValueError(text)
    pre = tuple(match.group(4).split(".")) if match.group(4) else ()
    pre_key = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), 0 if pre else 1, pre_key)


def spec_conformance() -> tuple[list[str], str]:
    conforms_to = (ROOT / "SPEC_VERSION").read_text().strip()
    package = (ROOT / "build-package/VERSION").read_text().strip()
    releases = RELEASE_HEADING.findall((ROOT / "build-package/CHANGELOG.md").read_text())
    if not SEMVER.match(conforms_to):
        return [f"SPEC_VERSION {conforms_to!r} is not SemVer 2.0.0"], ""
    if conforms_to not in releases:
        return [f"SPEC_VERSION {conforms_to} is not a released build-package version"], ""
    if precedence_key(conforms_to) > precedence_key(package):
        return [f"SPEC_VERSION {conforms_to} is newer than build-package/VERSION {package}"], ""
    if conforms_to != package:
        return [], f"conforms to spec {conforms_to}; spec is at {package} (implementation behind)"
    return [], f"conforms to spec {conforms_to} (current)"


def main(argv: list[str]) -> int:
    version = (ROOT / "VERSION").read_text().strip()
    problems = [] if SEMVER.match(version) else [f"VERSION {version!r} is not SemVer 2.0.0"]
    for where, value in stated_versions().items():
        if value != version:
            problems.append(f"{where} states {value!r}, VERSION is {version!r}")
    spec_problems, spec_note = spec_conformance()
    problems.extend(spec_problems)
    if "--tag" in argv:
        tag = argv[argv.index("--tag") + 1]
        if tag != f"v{version}":
            problems.append(f"tag {tag!r} does not match v{version}")
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        print(f"{len(problems)} problem(s)")
    else:
        print(f"application version {version} consistent; {spec_note}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
