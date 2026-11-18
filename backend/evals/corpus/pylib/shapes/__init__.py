"""Public API of the shapes package."""

from shapes.geometry import Circle, Rectangle, Square, total_area
from shapes.parse import parse_shape

__all__ = ["Circle", "Rectangle", "Square", "parse_shape", "total_area"]
