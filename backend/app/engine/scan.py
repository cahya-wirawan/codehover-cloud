"""Safe scan: classify every file without running anything (01 FR-005, 08 "Hard exclusions").

Order matters: hard exclusions (secret-bearing paths, VCS and dependency directories) are decided by
path alone, before content is looked at; then binary/oversize/line limits; then high-confidence secret
patterns, which block the file so its content never reaches a model, an artifact or search.
Heuristics do not guarantee secret detection; users must still review the selection (08).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from app.engine.acquire import MAX_FILE_BYTES, Entry

MAX_SCANNED_FILES = 2000
MAX_LINES = 3000
SNIFF_BYTES = 8192

LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",  # C++ instead when the snapshot has C++ sources (`scan`)
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".c++": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".h++": "cpp",
    ".ipp": "cpp",
    ".tpp": "cpp",
}
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "vendor",
        "dist",
        "build",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".next",
        "target",
        "coverage",
        ".idea",
        ".vscode",
    }
)
EXCLUDED_NAMES = frozenset(
    {
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".git-credentials",
        "credentials",
        ".htpasswd",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        ".dockercfg",
    }
)
EXCLUDED_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".tfstate", ".tfvars")
SAFE_ENV_EXAMPLES = frozenset({".env.example", ".env.sample", ".env.template"})
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY-----"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{60,}\b"),
    re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(rb"\bsk_live_[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"),
)


@dataclass(frozen=True)
class ScannedFile:
    path: str
    status: str  # selected | unsupported | excluded | binary | oversize | blocked
    reason: str | None
    language: str | None
    byte_count: int
    sha256: str | None = None
    line_count: int | None = None
    text: str | None = None  # kept for selected/unsupported text files only


def path_exclusion(path: str) -> str | None:
    pure = PurePosixPath(path)
    if any(part in EXCLUDED_DIRS for part in pure.parts[:-1]):
        return "excluded_directory"
    name = pure.name.lower()
    if name == ".env" or (name.startswith(".env.") and name not in SAFE_ENV_EXAMPLES):
        return "environment_file"
    if name in EXCLUDED_NAMES or name.endswith(EXCLUDED_SUFFIXES) or name.startswith("id_rsa"):
        return "credential_file"
    if pure.parts[:2] == (".aws", "credentials") or (".aws/" in path and name == "credentials"):
        return "credential_file"
    return None


def classify(entry: Entry) -> ScannedFile:
    if entry.skipped:
        return ScannedFile(entry.path, "excluded", entry.skipped, None, entry.size)
    excluded = path_exclusion(entry.path)
    if excluded:
        return ScannedFile(entry.path, "excluded", excluded, None, entry.size)  # content never inspected
    if entry.data is None or entry.size > MAX_FILE_BYTES:
        return ScannedFile(entry.path, "oversize", "file_too_large", None, entry.size)
    data = entry.data
    if b"\x00" in data[:SNIFF_BYTES]:
        return ScannedFile(entry.path, "binary", "binary_content", None, entry.size)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ScannedFile(entry.path, "binary", "not_utf8", None, entry.size)
    if any(pattern.search(data) for pattern in SECRET_PATTERNS):
        return ScannedFile(entry.path, "blocked", "secret_detected", None, entry.size)
    lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    if lines > MAX_LINES:
        return ScannedFile(entry.path, "oversize", "too_many_lines", None, entry.size)
    language = LANGUAGES.get(PurePosixPath(entry.path).suffix.lower())
    return ScannedFile(
        entry.path,
        "selected" if language else "unsupported",
        None if language else "language_not_supported",
        language,
        entry.size,
        hashlib.sha256(data).hexdigest(),
        lines,
        text,
    )


def scan(entries: list[Entry]) -> list[ScannedFile]:
    results: list[ScannedFile] = []
    for index, entry in enumerate(sorted(entries, key=lambda e: e.path)):
        if index >= MAX_SCANNED_FILES:
            results.append(ScannedFile(entry.path, "excluded", "file_limit", None, entry.size))
        else:
            results.append(classify(entry))
    # `.h` is C or C++ by convention only: in a snapshot with C++ sources, headers are read as C++, whose
    # grammar also reads C declarations, so classes and namespaces in them parse.
    if any(f.language == "cpp" for f in results):
        results = [
            replace(f, language="cpp") if f.language == "c" and f.path.lower().endswith(".h") else f
            for f in results
        ]
    return results
