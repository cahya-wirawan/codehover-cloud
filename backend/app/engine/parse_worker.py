"""Parser entry point, run inside the sandbox (`python -I -m app.engine.parse_worker`).

stdin:  {"files": [{"file_id": str, "path": str, "language": str, "text": str}]}
stdout: {"files": [{"file_id": str, "symbols": [...], "references": [...], "error": str | null}]}

Repository content is data: nothing here executes it. Positions follow 02/06: lines 1-based inclusive,
columns 0-based end-exclusive in Unicode scalar values (tree-sitter reports UTF-8 byte columns).
A file that fails to parse yields `error` and no symbols; the viewer still shows its source.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from typing import Any

from tree_sitter import Language, Node, Parser

MAX_SYMBOLS_PER_FILE = 2000
MAX_REFERENCES_PER_FILE = 20000


def _languages() -> dict[str, Language]:
    import tree_sitter_javascript
    import tree_sitter_python
    import tree_sitter_typescript

    return {
        "python": Language(tree_sitter_python.language()),
        "javascript": Language(tree_sitter_javascript.language()),
        "typescript": Language(tree_sitter_typescript.language_typescript()),
        "tsx": Language(tree_sitter_typescript.language_tsx()),
    }


PY_DEFINITIONS = {"function_definition": "function", "class_definition": "class"}
JS_DEFINITIONS = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "method_definition": "method",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
}
IDENTIFIER_TYPES = {"identifier", "type_identifier"}


class _Positions:
    def __init__(self, source: bytes) -> None:
        self.lines = source.split(b"\n")

    def column(self, row: int, byte_column: int) -> int:
        line = self.lines[row] if row < len(self.lines) else b""
        return len(line[:byte_column].decode("utf-8", "replace"))

    def span(self, node: Node) -> dict[str, int]:
        (sr, sc), (er, ec) = node.start_point, node.end_point
        return {
            "start_line": sr + 1,
            "end_line": er + 1,
            "start_column": self.column(sr, sc),
            "end_column": self.column(er, ec),
        }


def _text(node: Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None and node.text is not None else ""


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _python_docstring(node: Node) -> str | None:
    body = node.child_by_field_name("body")
    if body is None or not body.children:
        return None
    first = body.children[0]
    if first.type == "expression_statement" and first.children and first.children[0].type == "string":
        raw = _text(first.children[0]).strip("\"' \n\r\t")
        for prefix in ('"""', "'''"):
            raw = raw.strip(prefix)
        return raw.strip() or None
    return None


def _parameters(node: Node) -> list[str]:
    params = node.child_by_field_name("parameters")
    if params is None:
        return []
    names: list[str] = []
    for child in params.children:
        target: Node | None = child
        if child.type not in IDENTIFIER_TYPES:
            target = (
                child.child_by_field_name("name")
                or child.child_by_field_name("pattern")
                or next((c for c in child.children if c.type == "identifier"), None)
            )
        name = _text(target)
        if name and name not in ("self", "cls") and name.isidentifier():
            names.append(name)
    return names


def _signature(node: Node, source: bytes) -> str:
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    head = source[node.start_byte : end].decode("utf-8", "replace")
    return " ".join(head.split()).rstrip(" :{")[:300]


def parse_file(language: Language, kind: str, text: str) -> dict[str, Any]:
    source = text.encode("utf-8")
    tree = Parser(language).parse(source)
    positions = _Positions(source)
    definitions = PY_DEFINITIONS if kind == "python" else JS_DEFINITIONS
    symbols: list[dict[str, Any]] = []
    definition_name_nodes: set[tuple[int, int]] = set()
    seen_keys: dict[str, int] = {}

    def visit(node: Node, parents: list[str], in_class: bool) -> None:
        for child in node.children:
            symbol_kind = definitions.get(child.type)
            name_node = child.child_by_field_name("name") if symbol_kind else None
            # JS/TS `const f = () => ...` and `const f = function () {...}`
            if child.type == "variable_declarator":
                value = child.child_by_field_name("value")
                if value is not None and value.type in ("arrow_function", "function_expression", "function"):
                    symbol_kind, name_node = "function", child.child_by_field_name("name")
            if symbol_kind and name_node is not None and len(symbols) < MAX_SYMBOLS_PER_FILE:
                name = _text(name_node)
                effective = "method" if (in_class and symbol_kind == "function") else symbol_kind
                qualified = ".".join([*parents, name])
                count = seen_keys.get(qualified, 0)
                seen_keys[qualified] = count + 1
                key = qualified if count == 0 else f"{qualified}#{count + 1}"
                definition_name_nodes.add((name_node.start_byte, name_node.end_byte))
                target = child if child.type != "variable_declarator" else child.child_by_field_name("value")
                symbols.append(
                    {
                        "symbol_key": key,
                        "name": name,
                        "kind": effective,
                        "parent_key": ".".join(parents) or None,
                        **positions.span(child),
                        "signature": _signature(target or child, source),
                        "docstring": _python_docstring(child) if kind == "python" else None,
                        "params": _parameters(target or child),
                    }
                )
                visit(child, [*parents, name], in_class=effective == "class")
            else:
                visit(child, parents, in_class)

    visit(tree.root_node, [], in_class=False)
    references = []
    for node in _walk(tree.root_node):
        if node.type in IDENTIFIER_TYPES and (node.start_byte, node.end_byte) not in definition_name_nodes:
            row, col = node.start_point
            references.append({"name": _text(node), "line": row + 1, "column": positions.column(row, col)})
            if len(references) >= MAX_REFERENCES_PER_FILE:
                break
    return {
        "symbols": symbols,
        "references": references,
        "error": "syntax_error" if tree.root_node.has_error else None,
    }


def main() -> int:
    request = json.loads(sys.stdin.read())
    languages = _languages()
    results = []
    for item in request["files"]:
        language = languages.get(item["language"])
        if language is None:
            results.append(
                {"file_id": item["file_id"], "symbols": [], "references": [], "error": "unsupported"}
            )
            continue
        try:
            parsed = parse_file(language, item["language"], item["text"])
        except Exception:  # a parser crash on one file must not lose the others
            parsed = {"symbols": [], "references": [], "error": "parser_failure"}
        results.append({"file_id": item["file_id"], **parsed})
    sys.stdout.write(json.dumps({"files": results}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
