"""Read-only proposal status and scaffold operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

from mem_core.errors import ensure

from .models import ProposalManifest
from .paths import ProposalPaths

if TYPE_CHECKING:
    from ..workspace import Workspace


class ProposalService:
    """Application-facing proposal operations.

    Construction deliberately derives paths only. It does not construct a
    transaction manager, create temporary state, or reserve proposal IDs.
    """

    def __init__(self, workspace: "Workspace") -> None:
        self.workspace = workspace
        self.paths = ProposalPaths(workspace.paths)

    def status(self) -> dict[str, Any]:
        active = self.paths.active_proposals()
        current = None
        if active:
            current = {"review_id": active[0].name, "path": str(active[0])}
        return {"ok": True, "active_proposal": current}

    def scaffold(self) -> dict[str, Any]:
        ensure(
            not self.paths.active_proposals(),
            "PROPOSAL_ALREADY_ACTIVE",
            "proposal_scaffold",
            "An active proposal already exists",
            recovery=["Resume or abort the current active proposal"],
            canonical_changed=False,
            retry_safe=False,
        )
        identity = self.workspace.identity
        if callable(identity):
            identity = identity()
        manifest = ProposalManifest(
            review_id=f"review_{uuid.uuid4().hex}",
            base_commit=self.workspace.repository.head(),
            workspace_version=identity["workspace_format_version"],
            profile_version=self.workspace.profile.version,
        )
        return {"ok": True, "manifest": manifest.model_dump(mode="json")}
