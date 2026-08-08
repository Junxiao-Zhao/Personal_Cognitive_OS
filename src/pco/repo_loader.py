from __future__ import annotations

from pathlib import Path

from mem_core.profile import Profile
from mem_core.registry import default_registry
from mem_core.repository import MemoryRepository

from .paths import bundled_profile


def profile_for_repo(repo_root: Path) -> Profile:
    canonical = repo_root / "profiles" / "pco"
    return Profile.load(canonical if canonical.exists() else bundled_profile(), default_registry())


def repository_for_repo(repo_root: Path) -> MemoryRepository:
    return MemoryRepository(repo_root, profile_for_repo(repo_root))
