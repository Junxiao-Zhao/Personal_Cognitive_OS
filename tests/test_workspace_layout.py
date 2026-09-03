from __future__ import annotations

from pathlib import Path

from pco.paths import WorkspacePaths


def test_workspace_paths_are_fixed(tmp_path: Path) -> None:
    paths = WorkspacePaths.from_root(tmp_path / "memory")

    assert paths.root == tmp_path / "memory"
    assert paths.canonical == tmp_path / "memory/.pco/canonical"
    assert paths.indexes == tmp_path / "memory/.pco/indexes"
    assert paths.tmp == tmp_path / "memory/.pco/tmp"
    assert paths.proposals == tmp_path / "memory/PCO_PROPOSALS"
    assert paths.projection == tmp_path / "memory/PCO_MEMORY"
    assert paths.skills == tmp_path / "memory/.agents/skills"
