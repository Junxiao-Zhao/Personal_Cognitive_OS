from __future__ import annotations

import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from mem_core.profile import Profile
from mem_core.registry import default_registry
from mem_core.repository import MemoryRepository

from .paths import bundled_profile


def profile_for_repo(repo_root: Path) -> Profile:
    canonical = repo_root / "profiles" / "pco"
    return Profile.load(canonical if canonical.exists() else bundled_profile(), default_registry())


def repository_for_repo(repo_root: Path) -> MemoryRepository:
    return MemoryRepository(repo_root, profile_for_repo(repo_root))


@contextmanager
def repository_at(repo_root: Path, commit: str | None) -> Iterator[MemoryRepository]:
    """Open a read-only repository snapshot at ``commit``.

    Derivations normally run before the audit commit, but retries run after
    that commit exists at HEAD.  A temporary ``git archive`` keeps retries
    pinned to the content commit without mutating the live worktree.
    """
    repo_root = Path(repo_root)
    current = repository_for_repo(repo_root)
    if commit is None or commit == current.head():
        yield current
        return
    with tempfile.TemporaryDirectory(prefix="pco-derivation-") as directory:
        archive_path = Path(directory) / "snapshot.tar"
        with archive_path.open("wb") as archive:
            subprocess.run(
                ["git", "-C", str(repo_root), "archive", commit],
                stdout=archive,
                stderr=subprocess.PIPE,
                check=True,
            )
        snapshot_root = Path(directory) / "tree"
        snapshot_root.mkdir()
        with tarfile.open(archive_path) as bundle:
            try:
                # Python 3.12+ supports the safer extraction filter.
                bundle.extractall(snapshot_root, filter="data")
            except TypeError:
                # The project supports Python 3.11, whose tarfile API has no
                # filter parameter. The archive is produced by `git archive`
                # from the trusted local repository, so use the legacy API.
                bundle.extractall(snapshot_root)
        yield repository_for_repo(snapshot_root)
