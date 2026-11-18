"""Bounded extraction of a GitHub tarball or an uploaded ZIP (01 "Initial limits", 08 "Archive traversal").

Every limit is enforced while streaming, before anything is kept: compressed bytes, expanded bytes,
entry count and path depth. Traversal (absolute paths, `..`), NUL bytes and duplicate paths reject the
whole archive (they never occur in a genuine GitHub tarball). Symlinks, hard links, devices and FIFOs are
skipped and recorded, never followed or created. Nothing is written to disk.
"""

from __future__ import annotations

import io
import stat
import tarfile
import zipfile
from dataclasses import dataclass, field
from typing import IO

MAX_COMPRESSED_BYTES = 100 * 1024 * 1024
MAX_EXPANDED_BYTES = 250 * 1024 * 1024
MAX_ENTRIES = 10_000
MAX_DEPTH = 30
MAX_FILE_BYTES = 512 * 1024  # larger files are recorded as oversize, without content
MAX_KEPT_BYTES = 64 * 1024 * 1024  # content kept in memory for scanning


class ArchiveRejected(Exception):
    """The archive violates a safety rule. `code` is stored as the snapshot rejection code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    data: bytes | None  # None when not kept (oversize or over the kept-bytes budget)
    skipped: str | None = None  # "link" | "special" when not a regular file


@dataclass
class Extraction:
    entries: list[Entry] = field(default_factory=list)
    expanded_bytes: int = 0


class _CountingReader(io.RawIOBase):
    """Caps how many compressed bytes the decompressor may read."""

    def __init__(self, source: IO[bytes], limit: int) -> None:
        self._source = source
        self._limit = limit
        self.read_bytes = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:  # type: ignore[override]
        chunk = self._source.read(len(buffer))
        self.read_bytes += len(chunk)
        if self.read_bytes > self._limit:
            raise ArchiveRejected("compressed_too_large")
        buffer[: len(chunk)] = chunk
        return len(chunk)


def normalize(raw: str) -> str:
    """Strip GitHub's `{owner}-{repo}-{sha}/` prefix; reject anything that is not a clean relative path."""
    if "\x00" in raw or "\\" in raw:
        raise ArchiveRejected("invalid_path")
    if raw.startswith("/"):
        raise ArchiveRejected("path_traversal")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise ArchiveRejected("path_traversal")
    relative = parts[1:]  # drop the top-level directory GitHub adds
    if len(relative) > MAX_DEPTH:
        raise ArchiveRejected("path_too_deep")
    return "/".join(relative)


def extract(stream: IO[bytes]) -> Extraction:
    reader = _CountingReader(stream, MAX_COMPRESSED_BYTES)
    result = Extraction()
    seen: set[str] = set()
    kept = 0
    try:
        with tarfile.open(fileobj=io.BufferedReader(reader), mode="r|gz") as archive:
            for count, member in enumerate(archive, start=1):
                if count > MAX_ENTRIES:
                    raise ArchiveRejected("too_many_entries")
                path = normalize(member.name)
                if not path or member.isdir():
                    continue
                if path in seen:
                    raise ArchiveRejected("duplicate_path")
                seen.add(path)
                if member.issym() or member.islnk():
                    result.entries.append(Entry(path, 0, None, skipped="link"))
                    continue
                if not member.isfile():
                    result.entries.append(Entry(path, 0, None, skipped="special"))
                    continue
                result.expanded_bytes += member.size
                if result.expanded_bytes > MAX_EXPANDED_BYTES:
                    raise ArchiveRejected("expanded_too_large")
                if member.size > MAX_FILE_BYTES or kept + member.size > MAX_KEPT_BYTES:
                    result.entries.append(Entry(path, member.size, None))
                    continue
                handle = archive.extractfile(member)
                data = handle.read(MAX_FILE_BYTES + 1) if handle else b""
                if len(data) != member.size:  # header lied about the size
                    raise ArchiveRejected("size_mismatch")
                kept += len(data)
                result.entries.append(Entry(path, member.size, data))
    except ArchiveRejected:
        raise
    except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
        raise ArchiveRejected("corrupt_archive") from exc
    return result


# ---------------------------------------------------------------- uploaded ZIP archives (M6)
ZIP_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)


def _zip_path(raw: str) -> list[str]:
    if "\x00" in raw or "\\" in raw:
        raise ArchiveRejected("invalid_path")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise ArchiveRejected("path_traversal")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise ArchiveRejected("path_traversal")
    return parts


def extract_zip(data: bytes) -> Extraction:
    """An uploaded repository ZIP, with the tarball's limits. One shared top-level directory (as in
    GitHub's "Download ZIP") is stripped. Expanded sizes are counted from the bytes actually inflated,
    never trusted from headers, so a ZIP bomb stops at the limit. Encrypted entries, compression methods
    other than stored and deflate, traversal and duplicate paths reject the archive; symlinks are skipped."""
    if len(data) > MAX_COMPRESSED_BYTES:
        raise ArchiveRejected("compressed_too_large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise ArchiveRejected("corrupt_archive") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ArchiveRejected("too_many_entries")
        paths = [_zip_path(info.filename) for info in infos]
        files = [parts for info, parts in zip(infos, paths, strict=True) if parts and not info.is_dir()]
        roots = {parts[0] for parts in files}
        strip = 1 if len(roots) == 1 and all(len(parts) > 1 for parts in files) else 0
        result = Extraction()
        seen: set[str] = set()
        kept = 0
        for info, parts in zip(infos, paths, strict=True):
            relative = parts[strip:]
            if not relative or info.is_dir():
                continue
            if len(relative) > MAX_DEPTH:
                raise ArchiveRejected("path_too_deep")
            path = "/".join(relative)
            if path in seen:
                raise ArchiveRejected("duplicate_path")
            seen.add(path)
            if info.flag_bits & 0x1:
                raise ArchiveRejected("encrypted_entry")
            kind = stat.S_IFMT(info.external_attr >> 16)  # 0 when the archiver recorded no Unix type
            if kind == stat.S_IFLNK:
                result.entries.append(Entry(path, 0, None, skipped="link"))
                continue
            if kind not in (0, stat.S_IFREG):
                result.entries.append(Entry(path, 0, None, skipped="special"))
                continue
            if info.compress_type not in ZIP_METHODS:
                raise ArchiveRejected("unsupported_compression")
            budget = MAX_EXPANDED_BYTES - result.expanded_bytes
            keep = info.file_size <= MAX_FILE_BYTES and kept + info.file_size <= MAX_KEPT_BYTES
            size, content = _inflate(archive, info, budget, keep)
            result.expanded_bytes += size
            if size != info.file_size:  # the header lied about the size
                raise ArchiveRejected("size_mismatch")
            if content is not None:
                kept += size
            result.entries.append(Entry(path, size, content))
        return result


def _inflate(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, budget: int, keep: bool
) -> tuple[int, bytes | None]:
    """Inflate one entry in chunks, stopping as soon as the archive's expanded budget is exceeded."""
    size, chunks = 0, []
    try:
        with archive.open(info) as handle:
            while chunk := handle.read(64 * 1024):
                size += len(chunk)
                if size > budget:
                    raise ArchiveRejected("expanded_too_large")
                if keep and size <= MAX_FILE_BYTES:
                    chunks.append(chunk)
    except ArchiveRejected:
        raise
    except (zipfile.BadZipFile, EOFError, OSError, ValueError, NotImplementedError) as exc:
        raise ArchiveRejected("corrupt_archive") from exc
    return size, b"".join(chunks) if keep and size <= MAX_FILE_BYTES else None
