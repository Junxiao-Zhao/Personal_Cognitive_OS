from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    root: Path
    canonical: Path
    indexes: Path
    tmp: Path
    proposals: Path
    projection: Path
    skills: Path

    @classmethod
    def from_root(cls, root: Path) -> "WorkspacePaths":
        root = root.absolute()
        return cls(
            root,
            root / ".pco/canonical",
            root / ".pco/indexes",
            root / ".pco/tmp",
            root / "PCO_PROPOSALS",
            root / "PCO_MEMORY",
            root / ".agents/skills",
        )


def bundled_profile(name: str = "pco") -> Path:
    return Path(str(files("pco.resources.profiles").joinpath(name)))
