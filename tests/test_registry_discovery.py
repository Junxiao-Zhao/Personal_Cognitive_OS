import pytest

from mem_core.errors import MemError
from mem_core.registry import default_registry, discover_registry


def test_default_registry_discovers_pco_and_research_capabilities():
    registry = default_registry()
    assert callable(registry.resolve("pco.validate"))
    assert callable(registry.resolve("pco.retrieval.search"))
    assert callable(registry.resolve("pco.projection.affine"))
    assert callable(registry.resolve("research.retrieval.search"))


def test_discover_registry_unknown_group_resolves_nothing():
    registry = discover_registry(group="no.such.group")
    with pytest.raises(MemError) as exc:
        registry.resolve("anything")
    assert exc.value.detail.code == "ENTRYPOINT_NOT_ALLOWED"
