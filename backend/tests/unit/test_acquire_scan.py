"""Hostile-archive corpus and scan classification (13 "security": tar traversal/symlinks/bomb, A06, A07)."""

from __future__ import annotations

import io
import tarfile

import pytest

from app.engine import acquire
from app.engine.acquire import ArchiveRejected, Entry, extract
from app.engine.scan import classify, path_exclusion, scan

PREFIX = "octo-repo-0123abc/"


def tarball(*members: tuple[str, bytes | None, bytes]) -> io.BytesIO:
    """members: (name, data, type) with type tarfile.REGTYPE/SYMTYPE/LNKTYPE/CHRTYPE/DIRTYPE."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.SYMTYPE or kind == tarfile.LNKTYPE:
                info.linkname = "../../etc/passwd"
            if data is not None and kind == tarfile.REGTYPE:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            else:
                archive.addfile(info)
    buffer.seek(0)
    return buffer


def regular(name: str, data: bytes = b"x = 1\n") -> tuple[str, bytes, bytes]:
    return (PREFIX + name, data, tarfile.REGTYPE)


def test_github_prefix_is_stripped_and_files_kept() -> None:
    result = extract(
        tarball((PREFIX, None, tarfile.DIRTYPE), regular("src/app.py"), regular("README.md", b"# hi\n"))
    )
    assert [(e.path, e.data) for e in result.entries] == [
        ("src/app.py", b"x = 1\n"),
        ("README.md", b"# hi\n"),
    ]


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("/etc/passwd", "path_traversal"),
        (PREFIX + "../../etc/passwd", "path_traversal"),
        (PREFIX + "a/../../x", "path_traversal"),
        (PREFIX + "evil\\..\\x", "invalid_path"),
        (PREFIX + "/".join(["d"] * 31) + "/f.py", "path_too_deep"),
    ],
)
def test_traversal_and_bad_paths_reject_the_archive(name: str, code: str) -> None:
    with pytest.raises(ArchiveRejected) as rejected:
        extract(tarball((name, b"x", tarfile.REGTYPE)))
    assert rejected.value.code == code


def test_links_and_devices_are_skipped_not_followed() -> None:
    result = extract(
        tarball(
            regular("ok.py"),
            (PREFIX + "link", None, tarfile.SYMTYPE),
            (PREFIX + "hard", None, tarfile.LNKTYPE),
            (PREFIX + "dev", None, tarfile.CHRTYPE),
        )
    )
    assert {(e.path, e.skipped, e.data) for e in result.entries} == {
        ("ok.py", None, b"x = 1\n"),
        ("link", "link", None),
        ("hard", "link", None),
        ("dev", "special", None),
    }


def test_duplicate_paths_reject_the_archive() -> None:
    with pytest.raises(ArchiveRejected) as rejected:
        extract(tarball(regular("a.py"), regular("a.py", b"y = 2\n")))
    assert rejected.value.code == "duplicate_path"


def test_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acquire, "MAX_ENTRIES", 2)
    with pytest.raises(ArchiveRejected, match="too_many_entries"):
        extract(tarball(regular("a.py"), regular("b.py"), regular("c.py")))
    monkeypatch.setattr(acquire, "MAX_ENTRIES", 100)
    monkeypatch.setattr(acquire, "MAX_EXPANDED_BYTES", 1000)
    with pytest.raises(ArchiveRejected, match="expanded_too_large"):  # decompression bomb
        extract(tarball(regular("big.py", b"0" * 5000)))
    monkeypatch.setattr(acquire, "MAX_EXPANDED_BYTES", 10**9)
    monkeypatch.setattr(acquire, "MAX_COMPRESSED_BYTES", 50)
    with pytest.raises(ArchiveRejected, match="compressed_too_large"):
        extract(tarball(regular("a.py", bytes(range(256)) * 20)))


def test_oversize_file_kept_without_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acquire, "MAX_FILE_BYTES", 10)
    result = extract(tarball(regular("big.py", b"a = 1\n" * 10)))
    assert result.entries[0].data is None and result.entries[0].size == 60


def test_corrupt_archive() -> None:
    with pytest.raises(ArchiveRejected, match="corrupt_archive"):
        extract(io.BytesIO(b"\x1f\x8b not really gzip"))


# ---------------------------------------------------------------- scan
@pytest.mark.parametrize(
    ("path", "reason"),
    [
        (".env", "environment_file"),
        ("config/.env.production", "environment_file"),
        ("deploy/server.pem", "credential_file"),
        ("home/.ssh/id_rsa", "credential_file"),
        (".npmrc", "credential_file"),
        (".git/config", "excluded_directory"),
        ("web/node_modules/lib/index.js", "excluded_directory"),
    ],
)
def test_hard_exclusions_by_path(path: str, reason: str) -> None:
    assert path_exclusion(path) == reason
    assert classify(Entry(path, 10, b"SECRET=hunter2\n")).text is None  # content never kept


def test_safe_env_example_is_not_excluded() -> None:
    assert path_exclusion(".env.example") is None


@pytest.mark.parametrize(
    "content",
    [
        b"key = '-----BEGIN RSA PRIVATE KEY-----'\n",
        b"aws = 'AKIAABCDEFGHIJKLMNOP'\n",
        b"token = 'ghp_" + b"a" * 36 + b"'\n",
        b"stripe = 'sk_live_" + b"b" * 24 + b"'\n",
    ],
)
def test_high_confidence_secrets_block_the_file(content: bytes) -> None:
    scanned = classify(Entry("src/config.py", len(content), content))
    assert (scanned.status, scanned.reason, scanned.text) == ("blocked", "secret_detected", None)


def test_classification() -> None:
    results = {
        s.path: (s.status, s.language)
        for s in scan(
            [
                Entry("src/app.py", 6, b"x = 1\n"),
                Entry("web/main.tsx", 6, b"let a\n"),
                Entry("docs/guide.md", 4, b"# x\n"),
                Entry("img/logo.png", 4, b"\x89PNG\x00\x00"),
                Entry("latin.py", 3, "é\n".encode("latin-1")),
                Entry("big.py", 2_000_000, None),
                Entry("link", 0, None, skipped="link"),
            ]
        )
    }
    assert results == {
        "src/app.py": ("selected", "python"),
        "web/main.tsx": ("selected", "tsx"),
        "docs/guide.md": ("unsupported", None),
        "img/logo.png": ("binary", None),
        "latin.py": ("binary", None),
        "big.py": ("oversize", None),
        "link": ("excluded", None),
    }


def test_file_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.engine.scan as scan_module

    monkeypatch.setattr(scan_module, "MAX_SCANNED_FILES", 1)
    results = scan([Entry("a.py", 6, b"x = 1\n"), Entry("b.py", 6, b"y = 2\n")])
    assert [(r.path, r.status, r.reason) for r in results] == [
        ("a.py", "selected", None),
        ("b.py", "excluded", "file_limit"),
    ]
