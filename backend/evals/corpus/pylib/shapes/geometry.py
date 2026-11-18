"""Shapes with area and perimeter."""

import math


class Shape:
    """Base class; subclasses implement area and perimeter."""

    def area(self):
        raise NotImplementedError

    def perimeter(self):
        raise NotImplementedError

    def describe(self):
        """Human-readable summary using the subclass name."""
        return f"{type(self).__name__} with area {self.area():.2f}"


class Circle(Shape):
    def __init__(self, radius):
        if radius < 0:
            raise ValueError("radius must be non-negative")
        self.radius = radius

    def area(self):
        return math.pi * self.radius**2

    def perimeter(self):
        return 2 * math.pi * self.radius


class Rectangle(Shape):
    def __init__(self, width, height):
        self.width = width
        self.height = height

    def area(self):
        return self.width * self.height

    def perimeter(self):
        return 2 * (self.width + self.height)


class Square(Rectangle):
    """A rectangle with equal sides."""

    def __init__(self, side):
        super().__init__(side, side)


def total_area(shapes):
    """Sum of the areas; an empty input gives 0."""
    return sum(shape.area() for shape in shapes)


def scale(shape, factor):
    """Return a new shape scaled by factor. Only circles and rectangles are supported."""

    def scaled_rectangle(rect):
        return Rectangle(rect.width * factor, rect.height * factor)

    if isinstance(shape, Circle):
        return Circle(shape.radius * factor)
    return scaled_rectangle(shape)
