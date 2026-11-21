"""C and C++ (parser 0.5.0): definitions found through declarators, prototypes merged into their
definitions, doc comments, parameters, name-matched references, and `.h` read as C++ beside C++ sources."""

from __future__ import annotations

from typing import Any

from app.engine.acquire import Entry
from app.engine.parse_worker import _languages, parse_file
from app.engine.scan import scan

LANGUAGES = _languages()

C_SAMPLE = """#include <stdio.h>
#define MAX(a, b) ((a) > (b) ? (a) : (b))
#define VERSION 3

/* Forward declaration. */
static int helper(int x);

/**
 * A point in the plane.
 */
typedef struct point {
    int x, y;
    int (*cb)(int);
} point_t;

enum color { RED, GREEN };
typedef unsigned long size_type;
struct point *origin;

/// Adds two points.
point_t add(const point_t *a, point_t b) {
    point_t r = { a->x + b.x, a->y + b.y };
    return r;
}

static int helper(int x) { return MAX(x, 0); }
char **split(char *s, size_type n);
"""

CPP_SAMPLE = """namespace geo {
/// A shape.
class Shape {
public:
    virtual ~Shape();
    virtual double area() const = 0;
    void move(double dx, double dy = 0.0);
};

template <typename T>
// Sum of values.
T sum(const T& a, const T& b) { return a + b; }
}

double geo::Shape::area() const { return 0; }
void geo::Shape::move(double dx, double dy) { (void)dx; }
int scale(int v) { return v; }
double scale(double v) { return v; }
struct Point { int x; int norm() const { return x; } };
"""


def symbols(kind: str, source: str) -> dict[str, dict[str, Any]]:
    parsed = parse_file(LANGUAGES[kind], kind, source)
    assert parsed["error"] is None
    return {s["symbol_key"]: s for s in parsed["symbols"]}


def test_c_definitions_are_found_through_their_declarators() -> None:
    found = symbols("c", C_SAMPLE)
    assert {k: (s["kind"], s["parent_key"]) for k, s in found.items()} == {
        "MAX": ("function", None),  # a function-like macro; VERSION, a plain #define, is not a definition
        "point_t": ("type", None),
        "point": ("type", None),  # the struct a typedef defines inline stays at the outer level
        "color": ("type", None),
        "size_type": ("type", None),
        "add": ("function", None),
        "helper": ("function", None),  # its prototype merged into the definition
        "split": ("function", None),  # a prototype with no definition in this file
    }  # `struct point *origin` uses a struct; a field `int (*cb)(int)` is no function
    assert found["helper"]["start_line"] == 26 and found["helper"]["params"] == ["x"]
    assert found["add"]["params"] == ["a", "b"] and found["split"]["params"] == ["s", "n"]
    assert found["MAX"]["params"] == ["a", "b"]
    assert found["add"]["docstring"] == "Adds two points."
    assert found["point_t"]["docstring"] == "A point in the plane."
    assert found["add"]["signature"] == "point_t add(const point_t *a, point_t b)"
    assert found["split"]["signature"] == "char **split(char *s, size_type n)"


def test_cpp_classes_namespaces_methods_and_overloads() -> None:
    found = symbols("cpp", CPP_SAMPLE)
    assert {k: (s["kind"], s["parent_key"]) for k, s in found.items()} == {
        "geo": ("module", None),
        "geo.Shape": ("class", "geo"),
        "geo.Shape.~Shape": ("method", "geo.Shape"),  # declared only
        "geo.sum": ("function", "geo"),
        "geo.Shape.area": ("method", "geo.Shape"),  # defined outside the class, its declaration merged
        "geo.Shape.move": ("method", "geo.Shape"),
        "scale": ("function", None),
        "scale#2": ("function", None),  # an overload
        "Point": ("type", None),
        "Point.norm": ("method", "Point"),
    }
    assert found["geo.Shape.move"]["params"] == ["dx", "dy"] and found["geo.Shape.move"]["start_line"] == 16
    assert found["geo.sum"]["docstring"] == "Sum of values." and found["geo.Shape"]["docstring"] == "A shape."


def test_c_references_include_members_and_types_but_not_definition_names() -> None:
    parsed = parse_file(LANGUAGES["c"], "c", C_SAMPLE)
    names = {(r["name"], r["line"]) for r in parsed["references"]}
    assert ("MAX", 26) in names and ("point_t", 21) in names and ("x", 22) in names  # `a->x` is a member
    assert ("add", 21) not in names  # a definition's own name is not a use of it


def test_headers_are_cpp_beside_cpp_sources_and_c_otherwise() -> None:
    def languages(paths: list[str]) -> dict[str, str | None]:
        return {f.path: f.language for f in scan([Entry(p, 7, b"int x;\n") for p in paths])}

    assert languages(["lib.c", "lib.h"]) == {"lib.c": "c", "lib.h": "c"}
    assert languages(["lib.c", "lib.h", "app.cpp", "app.hpp"]) == {
        "app.cpp": "cpp",
        "app.hpp": "cpp",
        "lib.c": "c",
        "lib.h": "cpp",
    }
