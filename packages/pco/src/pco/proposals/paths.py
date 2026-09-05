"""No-follow discovery of the workspace's single active proposal."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
from typing import TYPE_CHECKING

from mem_core.errors import MemError

if TYPE_CHECKING:
    from ..paths import WorkspacePaths


_REVIEW_ID = re.compile(r"^review_[A-Za-z0-9._-]+$")


@dataclass(frozen=True, slots=True)
class ProposalPaths:
    """Proposal-owned paths derived without constructing mutable helpers."""

    workspace: "WorkspacePaths"

    @property
    def root(self) -> Path:
        return self.workspace.proposals

    @property
    def committed(self) -> Path:
        return self.root / ".committed"

    def active_proposals(self) -> tuple[Path, ...]:
        return active_proposals(self)


def active_proposals(
    paths: "WorkspacePaths | ProposalPaths | Path",
) -> tuple[Path, ...]:
    """Return zero or one immediate active directories without following links."""

    root = _proposal_root(paths)
    try:
        root_info = root.lstat()
    except OSError as exc:
        _unsafe(root, "Unable to inspect the proposal root", exc)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        _unsafe(root, "Proposal root must be a real directory")

    found: list[Path] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name == ".committed":
                    continue
                entry_path = root / entry.name
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    _unsafe(entry_path, "Unable to inspect proposal entry", exc)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    _unsafe(
                        entry_path,
                        "Active proposal entries must be real directories",
                    )
                if _REVIEW_ID.fullmatch(entry.name) is None:
                    _unsafe(
                        entry_path,
                        "Active proposal directory has an invalid review ID",
                    )
                found.append(entry_path)
    except MemError:
        raise
    except OSError as exc:
        _unsafe(root, "Unable to enumerate the proposal root", exc)

    found.sort(key=lambda path: path.name.encode("utf-8"))
    if len(found) > 1:
        raise MemError(
            "PROPOSAL_MANIFEST_MISMATCH",
            "proposal_discovery",
            "More than one active proposal exists",
            path=str(root),
            value=[path.name for path in found],
            recovery=["Keep exactly one active review directory under PCO_PROPOSALS"],
            canonical_changed=False,
            retry_safe=False,
        )
    return tuple(found)


def _proposal_root(paths: "WorkspacePaths | ProposalPaths | Path") -> Path:
    if isinstance(paths, ProposalPaths):
        return paths.root
    if isinstance(paths, Path):
        return paths
    return paths.proposals


def _unsafe(path: Path, message: str, exc: OSError | None = None) -> None:
    error = MemError(
        "PROPOSAL_PATH_UNSAFE",
        "proposal_discovery",
        f"{message}: {path}" if exc is None else f"{message}: {path}: {exc}",
        path=str(path),
        value=None if exc is None else str(exc),
        recovery=["Replace unsafe entries with one real review directory"],
        canonical_changed=False,
        retry_safe=False,
    )
    if exc is None:
        raise error
    raise error from exc
