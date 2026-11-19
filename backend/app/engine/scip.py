"""Read SCIP indexes (Sourcegraph's Code Intelligence Protocol, `scip.proto`) without a protobuf library.

Runs inside the sandbox (app.engine.index_worker): an index is derived from repository content, so it is
untrusted input. The reader decodes only the protobuf wire format and the few fields the reference graph
needs, checks every length against the buffer and refuses anything malformed with `ScipError`.

Field numbers (scip.proto): Index.documents = 2; Document.relative_path = 1, occurrences = 2,
position_encoding = 6; Occurrence.range = 1 (packed int32: [line, start, end] or
[line, start, end_line, end]), symbol = 2, symbol_roles = 3. Lines and characters are 0-based; characters
count UTF-8 bytes, UTF-16 code units or UTF-32 scalars per the document's encoding (unspecified means
UTF-16, which both shipped indexers use).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

DEFINITION = 0x1  # SymbolRole.Definition
UTF8, UTF16, UTF32 = 1, 2, 3
MAX_VARINT_BYTES = 10


class ScipError(ValueError):
    pass


@dataclass(frozen=True)
class Occurrence:
    line: int  # 0-based
    start: int  # 0-based, in the document's position encoding
    end_line: int
    end: int
    symbol: str
    roles: int

    @property
    def definition(self) -> bool:
        return bool(self.roles & DEFINITION)


@dataclass(frozen=True)
class Document:
    path: str
    encoding: int
    occurrences: list[Occurrence]


def _varint(data: memoryview, pos: int) -> tuple[int, int]:
    result = shift = 0
    for _ in range(MAX_VARINT_BYTES):
        if pos >= len(data):
            raise ScipError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, pos
        shift += 7
    raise ScipError("varint too long")


def _fields(data: memoryview) -> Iterator[tuple[int, int, int | memoryview]]:
    """(field number, wire type, value): an int for varints, a slice for length-delimited fields."""
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise ScipError("field number 0")
        if wire == 0:
            value, pos = _varint(data, pos)
            yield number, wire, value
        elif wire == 2:
            size, pos = _varint(data, pos)
            if size > len(data) - pos:
                raise ScipError("truncated field")
            yield number, wire, data[pos : pos + size]
            pos += size
        elif wire in (1, 5):  # fixed64 / fixed32: skipped, no field read here uses them
            pos += 8 if wire == 1 else 4
            if pos > len(data):
                raise ScipError("truncated field")
        else:
            raise ScipError(f"unsupported wire type {wire}")


def _int32(value: int) -> int:
    value &= 0xFFFFFFFFFFFFFFFF
    return value - (1 << 64) if value >= 1 << 63 else value


def _packed(data: memoryview) -> list[int]:
    values: list[int] = []
    pos = 0
    while pos < len(data):
        value, pos = _varint(data, pos)
        values.append(_int32(value))
    return values


def _text(value: int | memoryview) -> str:
    if not isinstance(value, memoryview):
        raise ScipError("expected a string")
    try:
        return bytes(value).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ScipError("invalid UTF-8") from exc


def _occurrence(data: memoryview) -> Occurrence | None:
    span: list[int] = []
    symbol = ""
    roles = 0
    for number, wire, value in _fields(data):
        if number == 1:
            if isinstance(value, memoryview):
                span.extend(_packed(value))
            else:  # an unpacked repeated int32 is also valid protobuf
                span.append(_int32(value))
        elif number == 2:
            symbol = _text(value)
        elif number == 3 and wire == 0 and isinstance(value, int):
            roles = value
    if len(span) == 3:
        span = [span[0], span[1], span[0], span[2]]
    if len(span) != 4 or min(span) < 0 or not symbol:
        return None  # malformed or anonymous: unusable, but not a reason to drop the document
    return Occurrence(span[0], span[1], span[2], span[3], symbol, roles)


def _document(data: memoryview, max_occurrences: int) -> Document:
    path = ""
    encoding = 0
    occurrences: list[Occurrence] = []
    for number, wire, value in _fields(data):
        if number == 1:
            path = _text(value)
        elif number == 2 and isinstance(value, memoryview):
            if len(occurrences) >= max_occurrences:
                raise ScipError("too many occurrences")
            occurrence = _occurrence(value)
            if occurrence is not None:
                occurrences.append(occurrence)
        elif number == 6 and wire == 0 and isinstance(value, int):
            encoding = value
    return Document(path, encoding or UTF16, occurrences)


def documents(index: bytes, *, max_occurrences: int = 200_000) -> list[Document]:
    """Every document of a serialized `scip.Index`, in index order."""
    result: list[Document] = []
    for number, _wire, value in _fields(memoryview(index)):
        if number == 2 and isinstance(value, memoryview):
            result.append(_document(value, max_occurrences))
    return result


def scalar_column(line: str, offset: int, encoding: int) -> int | None:
    """Convert a character offset in `encoding` to Unicode scalars (the product's columns, 02/06).
    None when the offset does not land on a character boundary inside the line."""
    if encoding == UTF32:
        return offset if offset <= len(line) else None
    units = 0
    for index, char in enumerate(line):
        if units == offset:
            return index
        if units > offset:
            return None
        code = ord(char)
        if encoding == UTF8:
            units += 1 if code < 0x80 else 2 if code < 0x800 else 3 if code < 0x10000 else 4
        else:
            units += 2 if code > 0xFFFF else 1
    return len(line) if units == offset else None
