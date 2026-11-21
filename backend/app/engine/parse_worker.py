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
    import tree_sitter_c
    import tree_sitter_cpp
    import tree_sitter_javascript
    import tree_sitter_python
    import tree_sitter_rust
    import tree_sitter_typescript

    return {
        "python": Language(tree_sitter_python.language()),
        "javascript": Language(tree_sitter_javascript.language()),
        "typescript": Language(tree_sitter_typescript.language_typescript()),
        "tsx": Language(tree_sitter_typescript.language_tsx()),
        "rust": Language(tree_sitter_rust.language()),
        "c": Language(tree_sitter_c.language()),
        "cpp": Language(tree_sitter_cpp.language()),
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
# Rust maps onto the product's kinds (symbols.kind): structs, enums and unions are types, traits are
# interfaces, inline modules are modules, constants and statics are variables, `macro_rules!` macros are
# functions. Functions inside an `impl` or a trait are methods; an `impl` block itself is not a symbol,
# its items are named after the implemented type (`Parser.new`).
RUST_DEFINITIONS = {
    "function_item": "function",
    "function_signature_item": "function",
    "struct_item": "type",
    "enum_item": "type",
    "union_item": "type",
    "type_item": "type",
    "trait_item": "interface",
    "mod_item": "module",
    "const_item": "variable",
    "static_item": "variable",
    "macro_definition": "function",
}
# C and C++ map onto the same kinds: functions (and function-like macros) are functions, or methods inside
# a class or qualified by one (`Parser::parse`); structs, unions, enums and typedefs are types; C++ classes
# are classes and namespaces modules. Names sit in declarators (`int *f(void)`), so C_DEFINITIONS lists the
# node types and `_c_definition` finds their names. A struct, union, enum or class counts only with a body
# (not `struct S *p;`), and a prototype only when its file has no definition of the same name.
C_DEFINITIONS = {
    "function_definition": "function",
    "declaration": "function",  # a prototype: `int f(int x);`
    "field_declaration": "function",  # a method declared in a class body
    "struct_specifier": "type",
    "union_specifier": "type",
    "enum_specifier": "type",
    "class_specifier": "class",
    "type_definition": "type",
    "namespace_definition": "module",
    "preproc_function_def": "function",
}
DEFINITIONS = {"python": PY_DEFINITIONS, "rust": RUST_DEFINITIONS, "c": C_DEFINITIONS, "cpp": C_DEFINITIONS}
IDENTIFIER_TYPES = {"identifier", "type_identifier"}
# Rust method calls and fields (`parser.next()`) name a `field_identifier`; so do C/C++ members (`p->next`).
REFERENCE_TYPES = {
    "rust": IDENTIFIER_TYPES | {"field_identifier"},
    "c": IDENTIFIER_TYPES | {"field_identifier"},
    "cpp": IDENTIFIER_TYPES | {"field_identifier"},
}
C_FAMILY = ("c", "cpp")
C_DECLARATORS = (
    "pointer_declarator",
    "reference_declarator",
    "parenthesized_declarator",
    "array_declarator",
    "attributed_declarator",
)
# Destructured parameters: `{ label }` in JS/TS, `Point { x }` in Rust.
SHORTHAND_PATTERNS = {"shorthand_property_identifier_pattern", "shorthand_field_identifier"}
# Fields of a parameter or pattern that bind no name: annotations, struct and variant names, defaults.
NON_BINDING_FIELDS = {"type", "value", "right", "key", "default_value"}


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


def _rust_docstring(node: Node) -> str | None:
    """The `///` or `/** */` doc comments above an item, skipping its attributes (`#[derive(...)]`)."""
    lines: list[str] = []
    sibling = node.prev_named_sibling
    while sibling is not None and sibling.type in ("line_comment", "block_comment", "attribute_item"):
        if sibling.type != "attribute_item":
            doc = next((c for c in sibling.children if c.type == "doc_comment"), None)
            outer = any(c.type == "outer_doc_comment_marker" for c in sibling.children)
            if doc is None or not outer:
                break
            text = _text(doc)
            if sibling.type == "block_comment":
                text = "\n".join(line.strip().removeprefix("*").strip() for line in text.splitlines())
            lines.insert(0, text.strip())
        sibling = sibling.prev_named_sibling
    return "\n".join(line for line in lines if line).strip() or None


def _c_docstring(node: Node) -> str | None:
    """The comments directly above a C/C++ definition, or above its `template <...>`, any style:
    `/** ... */`, `/* ... */`, `///` or `//`. A blank line ends the comment block."""
    found = _comments_above(node)
    if found is None and node.parent is not None and node.parent.type == "template_declaration":
        found = _comments_above(node.parent)
    return found


def _comments_above(anchor: Node) -> str | None:
    lines: list[str] = []
    row = anchor.start_point[0]
    sibling = anchor.prev_named_sibling
    while sibling is not None and sibling.type == "comment" and sibling.end_point[0] >= row - 1:
        text = _text(sibling).strip()
        if text.startswith("/*"):
            body = text.removeprefix("/**").removeprefix("/*").removesuffix("*/")
            block = [line.strip().removeprefix("*").strip() for line in body.splitlines()]
        else:
            block = [line.strip().lstrip("/").lstrip("!").strip() for line in text.splitlines()]
        lines[:0] = block
        row = sibling.start_point[0]
        sibling = sibling.prev_named_sibling
    return "\n".join(line for line in lines if line).strip() or None


def _function_declarator(node: Node | None) -> Node | None:
    """The `function_declarator` under a declarator chain (`*f(...)`, `&f(...)`), or None."""
    while node is not None:
        if node.type == "function_declarator":
            return node
        if node.type not in C_DECLARATORS:
            return None
        node = node.child_by_field_name("declarator")
    return None


def _c_definition(node: Node) -> tuple[Node, list[str], Node | None] | None:
    """(name node, qualifying scopes, function declarator) of a C/C++ definition, or None when the node
    defines nothing nameable (an anonymous struct, a use like `struct S *p`, a variable)."""
    if node.type in ("struct_specifier", "union_specifier", "enum_specifier", "class_specifier"):
        name = node.child_by_field_name("name")
        if name is None or node.child_by_field_name("body") is None:
            return None
        return name, [], None
    if node.type == "namespace_definition":
        name = node.child_by_field_name("name")
        return (name, [], None) if name is not None else None
    if node.type == "preproc_function_def":
        name = node.child_by_field_name("name")
        return (name, [], node) if name is not None else None
    if node.type == "type_definition":
        declarator = node.child_by_field_name("declarator")
        while declarator is not None and declarator.type != "type_identifier":
            declarator = declarator.child_by_field_name("declarator")
        return (declarator, [], None) if declarator is not None else None
    function = _function_declarator(node.child_by_field_name("declarator"))
    if function is None:
        return None  # a variable or field, not a function
    target = function.child_by_field_name("declarator")
    scopes: list[str] = []
    while target is not None and target.type == "qualified_identifier":  # `ns::Parser::parse`
        scope = target.child_by_field_name("scope")
        if scope is not None:
            scopes.append(_text(scope))
        target = target.child_by_field_name("name")
    if target is None or target.type not in (
        "identifier",
        "field_identifier",
        "destructor_name",
        "operator_name",
        "type_identifier",
    ):
        return None
    return target, scopes, function


def _rust_impl_name(node: Node) -> str | None:
    """The implemented type's name: `S` for `impl<T> Trait for path::S<T>` (or `&S`, `[S]`)."""
    target = node.child_by_field_name("type")
    for current in _walk(target) if target is not None else ():
        if current.type == "type_identifier":
            return _text(current)
    return None


def _bound_names(node: Node) -> list[str]:
    """The names a parameter binds, through destructuring: `{ label, a: b = 1 }` binds `label` and `b`,
    `(x, y)` and `Point { x, y: py }` bind `x`, `y` and `py`. Type annotations, default values, property
    keys and struct or variant names bind nothing."""
    if node.type in IDENTIFIER_TYPES or node.type in SHORTHAND_PATTERNS:
        return [_text(node)]
    if node.type in ("decorator", "self_parameter"):
        return []
    if node.type == "pair_pattern":  # `key: target`; only the target is bound
        value = node.child_by_field_name("value")
        return _bound_names(value) if value is not None else []
    names: list[str] = []
    for index, child in enumerate(node.children):
        if child.is_named and node.field_name_for_child(index) not in NON_BINDING_FIELDS:
            names.extend(_bound_names(child))
    return names


def _parameters(node: Node) -> list[str]:
    params = node.child_by_field_name("parameters")
    if params is None and node.type == "type_alias_declaration":  # `type F = (value: T) => U`
        value = node.child_by_field_name("value")
        if value is not None and value.type in ("function_type", "constructor_type"):
            params = value.child_by_field_name("parameters")
    if params is None:
        return []
    names: list[str] = []
    for child in params.named_children:
        for name in _bound_names(child):
            if name and name not in ("self", "cls") and name.isidentifier() and name not in names:
                names.append(name)
    return names


def _signature(node: Node, source: bytes, kind: str = "") -> str:
    if node.type == "macro_definition":  # the rules are the macro's body
        return f"macro_rules! {_text(node.child_by_field_name('name'))}"
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    head = source[node.start_byte : end].decode("utf-8", "replace")
    return " ".join(head.split()).rstrip(" :{;" if kind in ("rust", "c", "cpp") else " :{")[:300]


def parse_file(language: Language, kind: str, text: str) -> dict[str, Any]:
    source = text.encode("utf-8")
    tree = Parser(language).parse(source)
    positions = _Positions(source)
    definitions = DEFINITIONS.get(kind, JS_DEFINITIONS)
    identifiers = REFERENCE_TYPES.get(kind, IDENTIFIER_TYPES)
    symbols: list[dict[str, Any]] = []
    definition_name_nodes: set[tuple[int, int]] = set()
    seen_keys: dict[str, int] = {}

    def visit(node: Node, parents: list[str], in_class: bool) -> None:
        for child in node.children:
            if child.type == "impl_item":  # Rust: not a symbol; its items belong to the implemented type
                implemented = _rust_impl_name(child)
                body = child.child_by_field_name("body")
                if body is not None:
                    visit(body, [*parents, implemented] if implemented else parents, in_class=True)
                continue
            symbol_kind = definitions.get(child.type)
            name_node = child.child_by_field_name("name") if symbol_kind else None
            scopes: list[str] = []
            params_from: Node | None = None
            if symbol_kind and kind in C_FAMILY:
                found = _c_definition(child)
                name_node, scopes, params_from = found if found is not None else (None, [], None)
            # JS/TS `const f = () => ...` and `const f = function () {...}`
            if child.type == "variable_declarator":
                value = child.child_by_field_name("value")
                if value is not None and value.type in ("arrow_function", "function_expression", "function"):
                    symbol_kind, name_node = "function", child.child_by_field_name("name")
            if symbol_kind and name_node is not None and len(symbols) < MAX_SYMBOLS_PER_FILE:
                name = _text(name_node)
                effective = "method" if ((in_class or scopes) and symbol_kind == "function") else symbol_kind
                owners = [*parents, *scopes]
                qualified = ".".join([*owners, name])
                definition_name_nodes.add((name_node.start_byte, name_node.end_byte))
                target = child if child.type != "variable_declarator" else child.child_by_field_name("value")
                symbols.append(
                    {
                        "symbol_key": qualified,  # numbered below when a name repeats
                        "name": name,
                        "kind": effective,
                        "parent_key": ".".join(owners) or None,
                        **positions.span(child),
                        "signature": _signature(target or child, source, kind),
                        "docstring": _python_docstring(child)
                        if kind == "python"
                        else _rust_docstring(child)
                        if kind == "rust"
                        else _c_docstring(child)
                        if kind in C_FAMILY
                        else None,
                        "params": _parameters(params_from or target or child),
                        "prototype": child.type in ("declaration", "field_declaration") and kind in C_FAMILY,
                    }
                )
                in_type = effective == "class" or child.type in ("trait_item", "struct_specifier")
                # A typedef names its type; a struct it defines inline stays at the outer level.
                visit(child, owners if child.type == "type_definition" else [*owners, name], in_class=in_type)
            else:
                visit(child, parents, in_class)

    visit(tree.root_node, [], in_class=False)
    # A C/C++ prototype is a symbol only when its file does not also define it (`int f(int);` then
    # `int f(int x) {...}` is one function, documented at its definition).
    defined = {s["symbol_key"] for s in symbols if not s["prototype"]}
    symbols = [
        {k: v for k, v in s.items() if k != "prototype"}
        for s in symbols
        if not (s["prototype"] and s["symbol_key"] in defined)
    ]
    for s in symbols:  # a repeated name (overloads, `impl` blocks) gets `#2`, `#3`, ... in source order
        count = seen_keys.get(s["symbol_key"], 0)
        seen_keys[s["symbol_key"]] = count + 1
        if count:
            s["symbol_key"] = f"{s['symbol_key']}#{count + 1}"
    references = []
    for node in _walk(tree.root_node):
        if node.type in identifiers and (node.start_byte, node.end_byte) not in definition_name_nodes:
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
