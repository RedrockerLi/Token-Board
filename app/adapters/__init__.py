"""Adapter registry.

Each platform adapter registers itself via :func:`register_adapter`.
Callers use :func:`get_adapter` to obtain the right adapter for a
directory name (platform key).
"""

_ADAPTERS = {}


def register_adapter(cls):
    """Decorator / explicit registration of an adapter class."""
    instance = cls()
    _ADAPTERS[instance.platform] = instance
    return cls


def get_adapter(platform_name: str):
    """Return the adapter instance for *platform_name*, or ``None``."""
    return _ADAPTERS.get(platform_name)


def list_platforms():
    """Return sorted list of registered platform names."""
    return sorted(_ADAPTERS.keys())
