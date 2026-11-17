#!/usr/bin/env python3
"""Regenerate, version and validate the build package.

  python3 tools/assemble.py                     # regenerate master spec, manifest and version stamps
  python3 tools/assemble.py --check             # CI mode: fail on stale output or any structural problem
  python3 tools/assemble.py bump minor          # release Unreleased changelog entries as the next version
  python3 tools/assemble.py bump patch --date 2026-11-20

Versioning rules: 23_VERSIONING_AND_RELEASES.md. `VERSION` is the only hand-edited version;
README, master spec and manifest are stamped from it. Edit the numbered documents, never the
master spec. Requires PyYAML and jsonschema. SQL contracts are executed separately
(see VALIDATION_REPORT.md); this script does not need a database.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
MASTER = "CODEHOVER_WEB_MASTER_SPEC.md"
MANIFEST = "package_manifest.json"
CHANGELOG = "CHANGELOG.md"

# Official SemVer 2.0.0 pattern (semver.org), anchored.
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
RELEASE_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\] - (?P<date>\d{4}-\d{2}-\d{2})[ \t]*$", re.M)
UNRELEASED_HEADING = re.compile(r"^## \[Unreleased\][ \t]*$", re.M)
SCHEMA_ID = re.compile(r"^urn:codehover-cloud:schema:(?P<name>[a-z0-9-]+):(?P<version>.+)$")
README_STAMP = re.compile(r"^Version \S+ · [^·]+ ·", re.M)
REPORT_STAMP = re.compile(r"Package version (\S+)")

# Status labels for files the manifest tracks; anything new must be added here deliberately.
STATUS = {
    "doc": "complete specification",
    "template": "reference template/schema",
    "tooling": "package tooling",
    "history": "revision history",
}
EXTRA_FILES = {
    "README.md": "doc", "SOURCES.md": "doc", "CHANGELOG.md": "history", "VERSION": "tooling",
    "VALIDATION_REPORT.md": None,  # status text kept as written in the manifest
    "tools/assemble.py": "tooling",
}


# ---------- SemVer ----------

def parse_semver(text: str) -> tuple[int, int, int, tuple[str, ...]]:
    m = SEMVER.match(text)
    if not m:
        raise ValueError(f"not a SemVer 2.0.0 version: {text!r}")
    pre = tuple(m.group(4).split(".")) if m.group(4) else ()
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), pre


def precedence_key(text: str):
    """Sort key implementing SemVer precedence (build metadata ignored)."""
    major, minor, patch, pre = parse_semver(text)
    # A release (no pre-release) sorts after any pre-release of the same core version.
    pre_key = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre)
    return (major, minor, patch, 1 if not pre else 0, pre_key)


def bumped(version: str, part: str) -> str:
    major, minor, patch, _ = parse_semver(version)
    return {"major": f"{major + 1}.0.0", "minor": f"{major}.{minor + 1}.0", "patch": f"{major}.{minor}.{patch + 1}"}[part]


def date_label(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d.day} {d:%B %Y}"


# ---------- Changelog ----------

def read_version() -> str:
    return (ROOT / "VERSION").read_text().strip()


def changelog_releases(text: str) -> list[tuple[str, str]]:
    return [(m["version"], m["date"]) for m in RELEASE_HEADING.finditer(text)]


def unreleased_body(text: str) -> tuple[int, int, str]:
    m = UNRELEASED_HEADING.search(text)
    if not m:
        raise ValueError(f"{CHANGELOG}: missing '## [Unreleased]' section")
    nxt = RELEASE_HEADING.search(text, m.end())
    end = nxt.start() if nxt else len(text)
    return m.end(), end, text[m.end():end]


def bump(part: str, iso_date: str) -> str:
    current = read_version()
    new = bumped(current, part)
    text = (ROOT / CHANGELOG).read_text()
    start, end, body = unreleased_body(text)
    if not re.search(r"^\s*[-*] \S", body, re.M):
        raise SystemExit(f"{CHANGELOG}: Unreleased has no entries; nothing to release")
    released = f"\n\n## [{new}] - {iso_date}\n\n{body.strip()}\n\n"
    (ROOT / CHANGELOG).write_text(text[:start] + released + text[end:].lstrip("\n"))
    (ROOT / "VERSION").write_text(new + "\n")
    return new


# ---------- Generation ----------

def numbered_docs() -> list[str]:
    return sorted(p.name for p in ROOT.glob("[0-2][0-9]_*.md"))


def tracked_files() -> dict[str, str | None]:
    files: dict[str, str | None] = {name: "doc" for name in numbered_docs()}
    files.update(EXTRA_FILES)
    for sub in ("contracts", "deployment"):
        for p in sorted((ROOT / sub).rglob("*")):
            if p.is_file() and p.name not in (".DS_Store", ".env"):
                files[p.relative_to(ROOT).as_posix()] = "template"
    return dict(sorted(files.items()))


def release_date(version: str) -> str:
    for v, d in changelog_releases((ROOT / CHANGELOG).read_text()):
        if v == version:
            return d
    raise ValueError(f"{CHANGELOG}: no '## [{version}] - YYYY-MM-DD' section for VERSION {version}")


def stamped_readme(version: str, label: str) -> str:
    text = (ROOT / "README.md").read_text()
    if not README_STAMP.search(text):
        raise ValueError("README.md: missing 'Version X · date ·' line")
    return README_STAMP.sub(f"Version {version} · {label} ·", text, count=1)


def build_master(version: str, label: str) -> str:
    docs = numbered_docs()
    header = (
        "# CODEHOVER_WEB_MASTER_SPEC\n\n"
        f"Build specification v{version} · {label}\n\n"
        "Read this together with the archive for Docker/configuration and machine-readable contract files. "
        "This is a specification, not an implemented application. Generated by `tools/assemble.py`; "
        "edit the numbered documents instead.\n\n"
        "## Contents\n\n" + "".join(f"- {d}\n" for d in docs) + "\n"
    )
    parts = [(ROOT / f).read_text() for f in ["README.md", *docs, "SOURCES.md"]]
    return header + "\n\n---\n\n".join(parts)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------- Checks ----------

def version_checks(version: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        parse_semver(version)
    except ValueError as exc:
        return [f"VERSION: {exc}"], warnings
    text = (ROOT / CHANGELOG).read_text()
    try:
        unreleased_body(text)
    except ValueError as exc:
        errors.append(str(exc))
    releases = changelog_releases(text)
    versions = [v for v, _ in releases]
    for v in versions:
        if not SEMVER.match(v):
            errors.append(f"{CHANGELOG}: release heading {v!r} is not SemVer")
    if len(set(versions)) != len(versions):
        errors.append(f"{CHANGELOG}: duplicate release headings")
    valid = [v for v in versions if SEMVER.match(v)]
    if valid != sorted(valid, key=precedence_key, reverse=True):
        errors.append(f"{CHANGELOG}: releases are not in descending SemVer order")
    dates = [d for _, d in releases]
    if dates != sorted(dates, reverse=True):
        errors.append(f"{CHANGELOG}: release dates are not in descending order")
    if not versions or versions[0] != version:
        errors.append(f"VERSION {version} is not the newest changelog release ({versions[:1]})")
    report = REPORT_STAMP.search((ROOT / "VALIDATION_REPORT.md").read_text())
    if not report:
        errors.append("VALIDATION_REPORT.md: missing 'Package version X' stamp")
    elif report.group(1) != version:
        warnings.append(f"VALIDATION_REPORT.md describes {report.group(1)}; package is {version} (re-validate and update it)")
    for p in sorted((ROOT / "contracts").glob("*.schema.json")):
        schema = json.loads(p.read_text())
        m = SCHEMA_ID.match(schema.get("$id", ""))
        if not m:
            errors.append(f"{p.name}: $id must be urn:codehover-cloud:schema:<name>:<semver>")
            continue
        if not SEMVER.match(m["version"]):
            errors.append(f"{p.name}: $id version {m['version']!r} is not SemVer")
            continue
        major = parse_semver(m["version"])[0]
        title_major = re.search(r"\bv(\d+)\b", schema.get("title", ""))
        if not title_major or int(title_major.group(1)) != major:
            errors.append(f"{p.name}: title must state v{major} to match $id")
        sv = schema.get("properties", {}).get("schema_version", {}).get("const")
        if sv is not None and sv != major:
            errors.append(f"{p.name}: schema_version const {sv} must equal $id MAJOR {major}")
    return errors, warnings


def structural_checks() -> list[str]:
    errors: list[str] = []
    for p in sorted(ROOT.rglob("*.json")):
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"{p.name}: invalid JSON ({exc})")
            continue
        if p.name.endswith(".schema.json"):
            try:
                Draft202012Validator.check_schema(data)
            except Exception as exc:  # jsonschema.SchemaError
                errors.append(f"{p.name}: invalid Draft 2020-12 schema ({exc})")
    for p in sorted(ROOT.rglob("*.yaml")):
        try:
            yaml.safe_load(p.read_text())
        except yaml.YAMLError as exc:
            errors.append(f"{p.name}: invalid YAML ({exc})")
    for p in sorted(ROOT.rglob("*.sh")):
        if subprocess.run(["sh", "-n", str(p)], capture_output=True).returncode:
            errors.append(f"{p.name}: shell syntax error")
    for p in sorted(ROOT.rglob("*.md")):
        text = p.read_text()
        if text.count("```") % 2:
            errors.append(f"{p.name}: unbalanced code fences")
        if p.name not in ("CHANGELOG.md", "VALIDATION_REPORT.md") and re.search(r"\b(TODO|TBD)\b", text):
            errors.append(f"{p.name}: unresolved TODO/TBD marker")
    compose = yaml.safe_load((ROOT / "deployment/compose.yaml").read_text())
    for name, svc in compose["services"].items():
        for dep in (svc.get("depends_on") or {}):
            if dep not in compose["services"]:
                errors.append(f"compose: {name} depends on unknown service {dep}")
    return errors


# ---------- Main ----------

def write_or_flag(path: Path, content: str, check: bool, problems: list[str]) -> None:
    if path.read_text() != content:
        if check:
            problems.append(f"{path.name} is stale; run tools/assemble.py")
        else:
            path.write_text(content)


def regenerate(check: bool) -> int:
    version = read_version()
    problems, warnings = version_checks(version)
    problems += structural_checks()
    if any(p.startswith(("VERSION", CHANGELOG)) for p in problems):
        for p in problems:
            print(f"FAIL {p}")
        return 1
    label = date_label(release_date(version))

    write_or_flag(ROOT / "README.md", stamped_readme(version, label), check, problems)
    master_path = ROOT / MASTER
    write_or_flag(master_path, build_master(version, label), check, problems)

    manifest = json.loads((ROOT / MANIFEST).read_text())
    documents = {}
    for rel, kind in tracked_files().items():
        old = manifest["documents"].get(rel, {})
        status = old.get("status") if kind is None else STATUS[kind]
        documents[rel] = {"status": status or STATUS["doc"], "sha256": sha256(ROOT / rel)}
    new_manifest = dict(manifest, package_version=version, documents=documents,
                        master_spec_sha256=sha256(master_path))
    write_or_flag(ROOT / MANIFEST, json.dumps(new_manifest, indent=2, ensure_ascii=False) + "\n", check, problems)

    for w in warnings:
        print(f"WARN {w}")
    for p in problems:
        print(f"FAIL {p}")
    print(f"package {version} OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify only; exit 1 if anything is stale or invalid")
    sub = parser.add_subparsers(dest="command")
    b = sub.add_parser("bump", help="release Unreleased changelog entries as a new version")
    b.add_argument("part", choices=["major", "minor", "patch"])
    b.add_argument("--date", default=dt.date.today().isoformat(), help="release date, YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    if args.command == "bump":
        dt.date.fromisoformat(args.date)
        previous = read_version()
        new = bump(args.part, args.date)
        print(f"{previous} -> {new}")
        return regenerate(check=False)
    return regenerate(check=args.check)


if __name__ == "__main__":
    sys.exit(main())
