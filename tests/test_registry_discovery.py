from importlib.metadata import entry_points, version
from pathlib import Path
import tomllib

import pytest

import mem_core
import pco
from mem_core.errors import MemError
from mem_core.registry import default_registry, discover_registry

ROOT = Path(__file__).resolve().parents[1]


def test_default_registry_discovers_only_retained_v05_capabilities():
    registry = default_registry()
    retained = (
        "pco.validate",
        "pco.retrieval.search",
    )
    removed = (
        "pco.index.build",
        "pco.projection.markdown",
        "pco.projection.affine",
        "pco.backlinks.build",
        "pco.context.render",
        "research.validate",
        "research.retrieval.search",
        "research.projection.markdown",
    )
    for name in retained:
        assert callable(registry.resolve(name))
    for name in removed:
        with pytest.raises(MemError) as exc:
            registry.resolve(name)
        assert exc.value.detail.code == "ENTRYPOINT_NOT_ALLOWED"
    assert {entry.name for entry in entry_points(group="mem_core.capabilities")} == set(
        retained
    )


def test_source_and_package_metadata_versions_are_050():
    mem_project = tomllib.loads((ROOT / "packages/mem-core/pyproject.toml").read_text())
    pco_project = tomllib.loads((ROOT / "packages/pco/pyproject.toml").read_text())

    assert mem_core.__version__ == "0.5.0"
    assert pco.__version__ == "0.5.0"
    assert version("mem-core") == "0.5.0"
    assert version("pco") == "0.5.0"
    assert mem_project["project"]["version"] == "0.5.0"
    assert pco_project["project"]["version"] == "0.5.0"
    assert "mem-core>=0.5.0,<0.6.0" in pco_project["project"]["dependencies"]


def test_discover_registry_unknown_group_resolves_nothing():
    registry = discover_registry(group="no.such.group")
    with pytest.raises(MemError) as exc:
        registry.resolve("anything")
    assert exc.value.detail.code == "ENTRYPOINT_NOT_ALLOWED"
