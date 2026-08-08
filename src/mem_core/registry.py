from __future__ import annotations

from importlib import metadata

from .profile import ProfileRegistry


ENTRY_POINT_GROUP = "mem_core.capabilities"


def discover_registry(group: str = ENTRY_POINT_GROUP) -> ProfileRegistry:
    """Build an allowlist from installed entry points.

    The distribution that ships each Profile declares its capabilities under
    the ``mem_core.capabilities`` group. mem-core never imports domain
    modules by name, keeping it profile-neutral.
    """
    registry = ProfileRegistry()
    for entry in metadata.entry_points(group=group):
        registry.register_lazy(entry.name, entry.value)
    return registry


def default_registry() -> ProfileRegistry:
    return discover_registry()
