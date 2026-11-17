#!/usr/bin/env python3
"""Application version consistency (build-package/23_VERSIONING_AND_RELEASES.md).

VERSION is the source of truth; every other place that states the application version must agree.
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


def main(argv: list[str]) -> int:
    version = (ROOT / "VERSION").read_text().strip()
    problems = [] if SEMVER.match(version) else [f"VERSION {version!r} is not SemVer 2.0.0"]
    for where, value in stated_versions().items():
        if value != version:
            problems.append(f"{where} states {value!r}, VERSION is {version!r}")
    if "--tag" in argv:
        tag = argv[argv.index("--tag") + 1]
        if tag != f"v{version}":
            problems.append(f"tag {tag!r} does not match v{version}")
    for problem in problems:
        print(f"FAIL {problem}")
    print(f"application version {version} consistent" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
