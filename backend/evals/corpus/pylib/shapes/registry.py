"""A registry of shape factories, with decorators, properties and async helpers."""

import functools

_REGISTRY = {}


def register(name):
    """Decorator that records a factory under name; registering a name twice raises KeyError."""

    def decorator(factory):
        if name in _REGISTRY:
            raise KeyError(name)
        _REGISTRY[name] = factory
        return factory

    return decorator


def lookup(name):
    """Return the factory for name, or None when it is not registered."""
    return _REGISTRY.get(name)


class Catalog:
    """Read-only view of the registry."""

    class Entry:
        def __init__(self, name, factory):
            self.name = name
            self.factory = factory

    @property
    def names(self):
        return sorted(_REGISTRY)

    @staticmethod
    def size():
        return len(_REGISTRY)

    def entries(self):
        return [Catalog.Entry(n, f) for n, f in sorted(_REGISTRY.items())]


@functools.lru_cache(maxsize=32)
def cached_lookup(name):
    return lookup(name)


async def load_remote(name):
    """Placeholder: always returns the local factory; nothing is fetched."""
    return lookup(name)


async def warm_up(names):
    for name in names:
        await load_remote(name)
