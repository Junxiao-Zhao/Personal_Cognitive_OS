"""PCO v0.5 dedicated workspace initialization and opening."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from mem_core.errors import ensure
from mem_core.profile import Profile
from mem_core.registry import default_registry
from mem_core.repository import MemoryRepository

from . import __version__
from . import init_safety
from .config import PROFILE_VERSION, WORKSPACE_FORMAT_VERSION
from .init_safety import (
    canonical_json_bytes,
    claim_workspace,
    copy_tree_bytes_anchored,
)
from .paths import WorkspacePaths, bundled_profile
from .skill_resources import bundled_skill, verified_skill_tree_bytes


_BUNDLED_SKILLS = ("pco-memory", "pco-consolidate")


class Workspace:
    """A fixed-layout PCO workspace."""

    def __init__(
        self,
        paths: WorkspacePaths,
        profile: Profile,
        repository: MemoryRepository,
        identity: dict[str, Any],
    ) -> None:
        self.paths = paths
        self.root = paths.root
        self.profile_path = profile.root
        self.profile = profile
        self.repository = repository
        self.identity = identity

    @classmethod
    def init(cls, root: Path) -> dict[str, Any]:
        """Create a complete dedicated workspace through a stable root anchor."""

        init_safety.require_anchor_capabilities_before_mutation()
        paths = WorkspacePaths.from_root(Path(root))
        profile = Profile.load(bundled_profile(), default_registry())
        ensure(
            profile.version == PROFILE_VERSION,
            "WORKSPACE_FORMAT_MISMATCH",
            "init",
            "Bundled Profile version does not match the workspace format",
            canonical_changed=False,
            retry_safe=False,
        )

        skill_snapshots: dict[str, dict[str, bytes]] = {}
        for skill_name in _BUNDLED_SKILLS:
            source = Path(str(bundled_skill(skill_name)))
            metadata, snapshot = verified_skill_tree_bytes(source, __version__)
            ensure(
                metadata["name"] == skill_name,
                "WORKSPACE_SKILL_INVALID",
                "verify",
                "Bundled SKILL manifest name does not match its install location",
                path=skill_name,
                canonical_changed=False,
                retry_safe=False,
            )
            skill_snapshots[skill_name] = snapshot

        workspace_id = f"workspace_{uuid.uuid4().hex}"
        identity = {
            "workspace_id": workspace_id,
            "workspace_format_version": WORKSPACE_FORMAT_VERSION,
            "profile_version": PROFILE_VERSION,
        }
        repository = MemoryRepository(paths.canonical, profile, _resolve_root=False)

        with claim_workspace(paths.root) as anchor:
            with anchor.open_directory(".pco") as pco_root:
                anchor.assert_target_identity()
                pco_root.assert_target_identity()
                for relative in ("canonical", "indexes", "tmp"):
                    pco_root.mkdir(relative, parents=True)
                for relative in (
                    "PCO_PROPOSALS",
                    "PCO_MEMORY",
                    ".agents/skills",
                ):
                    anchor.mkdir(relative, parents=True)
                anchor.assert_target_identity()
                pco_root.assert_target_identity()

                with pco_root.open_directory("canonical") as canonical:
                    anchor.assert_target_identity()
                    pco_root.assert_target_identity()
                    commit = repository.init_anchored(
                        canonical,
                        initial_files={
                            "workspace.json": canonical_json_bytes(identity),
                        },
                    )
                anchor.assert_target_identity()
                pco_root.assert_target_identity()

                with anchor.open_directory(".agents/skills") as skills_root:
                    for skill_name in _BUNDLED_SKILLS:
                        anchor.assert_target_identity()
                        pco_root.assert_target_identity()
                        skills_root.assert_target_identity()
                        skills_root.mkdir(skill_name)
                        with skills_root.open_directory(skill_name) as skill_root:
                            copy_tree_bytes_anchored(
                                skill_root, skill_snapshots[skill_name]
                            )
                    anchor.assert_target_identity()
                    pco_root.assert_target_identity()
                    skills_root.assert_target_identity()

        return {
            "ok": True,
            "workspace": str(paths.root),
            "workspace_id": workspace_id,
            "memory_commit": commit,
        }

    @classmethod
    def open(cls, root: Path) -> "Workspace":
        """Open the fixed workspace layout without mutating it."""

        paths = WorkspacePaths.from_root(Path(root))
        identity_path = paths.canonical / "workspace.json"
        ensure(
            identity_path.is_file(),
            "WORKSPACE_NOT_INITIALIZED",
            "workspace",
            f"Not a PCO workspace: {paths.root}",
            canonical_changed=False,
            retry_safe=False,
        )
        profile_path = paths.canonical / "profiles/pco"
        profile = Profile.load(profile_path, default_registry())
        repository = MemoryRepository(paths.canonical, profile)
        repository.assert_initialized()
        try:
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            ensure(
                False,
                "WORKSPACE_NOT_INITIALIZED",
                "workspace",
                f"Invalid workspace identity: {identity_path}: {exc}",
                canonical_changed=False,
                retry_safe=False,
            )
            raise AssertionError("unreachable")
        ensure(
            isinstance(identity, dict),
            "WORKSPACE_NOT_INITIALIZED",
            "workspace",
            f"Invalid workspace identity: {identity_path}",
            canonical_changed=False,
            retry_safe=False,
        )
        return cls(paths, profile, repository, identity)

    def assert_initialized(self) -> None:
        self.repository.assert_initialized()
        ensure(
            (self.paths.canonical / "workspace.json").is_file(),
            "WORKSPACE_NOT_INITIALIZED",
            "workspace",
            f"Not a PCO workspace: {self.root}",
            canonical_changed=False,
            retry_safe=False,
        )
