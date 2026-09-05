"""Read-only integrity verification for a dedicated PCO workspace.

Projection verification intentionally is not implemented here.  The current
projection format is owned by the derivation plan; this module verifies only
the workspace state that exists independently of that future format.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mem_core.errors import MemError, ensure
from mem_core.repository import MemoryRepository
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import __version__
from .config import PROFILE_VERSION, PROPOSAL_MANIFEST_VERSION, WORKSPACE_FORMAT_VERSION
from .skill_resources import verify_skill_tree
from .versioning import check_compatibility

if TYPE_CHECKING:
    from .workspace import Workspace


_SKILL_NAMES = ("pco-memory", "pco-consolidate")
_INTEGRITY_CODE = "WORKSPACE_INTEGRITY_ERROR"
_PERMISSION_CONTRACT = {
    "read_commands_need_write_permission": False,
    "proposal_authoring": "one-time Host Edit permission",
    "canonical_submission": "separate one-time Host commit permission",
    "permanent_workspace_write_permission": "unsupported",
    # This is a Host/TUI behavioral observation, not a filesystem fact that a
    # doctor command can establish.
    "tui_discovery_or_approval_proven": False,
}


class WorkspaceIdentity(BaseModel):
    """The exact, immutable identity carried in canonical ``workspace.json``."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(pattern=r"^workspace_[0-9a-f]{32}$")
    workspace_format_version: str = Field(pattern=r"^0\.5\.0$")
    profile_version: str = Field(pattern=r"^0\.5\.0$")


def load_workspace_identity(repository: MemoryRepository) -> WorkspaceIdentity:
    """Read and anchor workspace identity to its exact initial Git blob."""

    current_path = repository.root / "workspace.json"
    try:
        current_bytes = current_path.read_bytes()
        current = WorkspaceIdentity.model_validate_json(current_bytes)
        root_commit = repository._git("rev-list", "--max-parents=0", "HEAD")
        root_bytes = repository._git("show", f"{root_commit}:workspace.json")
        original = WorkspaceIdentity.model_validate_json(root_bytes)
        current_blob = repository._git("hash-object", "workspace.json")
        original_blob = repository._git("rev-parse", f"{root_commit}:workspace.json")
    except (OSError, UnicodeDecodeError, ValidationError, MemError) as exc:
        if isinstance(exc, MemError) and exc.detail.code != "GIT_FAILED":
            raise
        _integrity_error("Unable to read a valid root-anchored workspace identity", exc)

    ensure(
        current == original and current_blob == original_blob,
        _INTEGRITY_CODE,
        "doctor",
        "workspace.json differs from the canonical root commit",
        canonical_changed=False,
        retry_safe=False,
        path="workspace.json",
        recovery=[
            "Restore the complete canonical repository containing its original initial commit"
        ],
    )
    _ensure_identity_versions(current)
    _ensure_transaction_receipts(repository, current)
    return current


def verify_workspace(
    workspace: "Workspace", path: Path | None = None
) -> dict[str, Any]:
    """Verify all currently defined workspace integrity boundaries without writes.

    ``path`` remains an explicit extension point for derivation verification.
    No projection verifier exists yet, so requesting ``PCO_MEMORY`` reports a
    structured deferral rather than treating an empty or legacy tree as valid.
    """

    identity = load_workspace_identity(workspace.repository)
    skills = _verify_local_skills(workspace)
    compatibility = _verify_compatibility(identity, skills)
    if path is not None:
        _verify_requested_path(workspace, path)
    return {
        "ok": True,
        "identity": identity.model_dump(mode="json"),
        "compatibility": compatibility,
        "skills": skills,
        "permission_contract": dict(_PERMISSION_CONTRACT),
    }


def _ensure_identity_versions(identity: WorkspaceIdentity) -> None:
    ensure(
        identity.workspace_format_version == WORKSPACE_FORMAT_VERSION
        and identity.profile_version == PROFILE_VERSION,
        _INTEGRITY_CODE,
        "doctor",
        "workspace.json has unsupported workspace or Profile versions",
        canonical_changed=False,
        retry_safe=False,
        path="workspace.json",
    )


def _ensure_transaction_receipts(
    repository: MemoryRepository, identity: WorkspaceIdentity
) -> None:
    transaction_path = repository.root / "transactions" / "transactions.jsonl"
    try:
        with transaction_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                transaction = json.loads(line)
                ensure(
                    isinstance(transaction, dict),
                    _INTEGRITY_CODE,
                    "doctor",
                    "Transaction record must be a JSON object",
                    canonical_changed=False,
                    retry_safe=False,
                    path=f"transactions/transactions.jsonl:{line_number}",
                )
                for receipt_name in ("receipt", "approval_receipt"):
                    receipt = transaction.get(receipt_name)
                    if receipt is None:
                        continue
                    ensure(
                        isinstance(receipt, dict),
                        _INTEGRITY_CODE,
                        "doctor",
                        "Transaction receipt must be a JSON object",
                        canonical_changed=False,
                        retry_safe=False,
                        path=f"transactions/transactions.jsonl:{line_number}",
                    )
                    # Historical records without the later workspace-scoped
                    # receipt field remain readable.  Once a receipt carries
                    # that field it must bind to this immutable workspace.
                    if "workspace_id" in receipt:
                        ensure(
                            receipt["workspace_id"] == identity.workspace_id,
                            _INTEGRITY_CODE,
                            "doctor",
                            "Receipt workspace identity mismatch",
                            canonical_changed=False,
                            retry_safe=False,
                            path=f"transactions/transactions.jsonl:{line_number}",
                        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _integrity_error("Unable to read canonical transaction receipts", exc)


def _verify_local_skills(workspace: "Workspace") -> dict[str, dict[str, Any]]:
    verified: dict[str, dict[str, Any]] = {}
    for name in _SKILL_NAMES:
        result = verify_skill_tree(workspace.paths.skills / name, __version__)
        ensure(
            result["name"] == name,
            "WORKSPACE_SKILL_INVALID",
            "verify",
            "SKILL manifest name does not match its workspace install location",
            canonical_changed=False,
            retry_safe=False,
            path=name,
        )
        verified[name] = result
    return verified


def _verify_compatibility(
    identity: WorkspaceIdentity, skills: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    skill_versions = tuple(skills[name]["version"] for name in _SKILL_NAMES)
    check_compatibility(
        package=__version__,
        workspace=identity.workspace_format_version,
        profile=identity.profile_version,
        proposal_manifest=PROPOSAL_MANIFEST_VERSION,
        skills=skill_versions,
    )
    return {
        "ok": True,
        "package": __version__,
        "workspace": identity.workspace_format_version,
        "profile": identity.profile_version,
        "proposal_manifest": PROPOSAL_MANIFEST_VERSION,
        "skills": dict(zip(_SKILL_NAMES, skill_versions, strict=True)),
    }


def _verify_requested_path(workspace: "Workspace", path: Path) -> None:
    requested = Path(path).absolute()
    if requested == workspace.paths.projection:
        raise MemError(
            "WORKSPACE_VERIFICATION_DEFERRED",
            "verify",
            "PCO_MEMORY verification is deferred until the derivation projection verifier exists",
            canonical_changed=False,
            retry_safe=False,
            path="PCO_MEMORY",
            recovery=["Use canonical pco search or pco history until derivations are available"],
        )
    raise MemError(
        "WORKSPACE_PATH_UNSAFE",
        "verify",
        f"Unsupported verification path: {path}",
        canonical_changed=False,
        retry_safe=False,
        path=str(path),
    )


def _integrity_error(message: str, exc: BaseException) -> None:
    raise MemError(
        _INTEGRITY_CODE,
        "doctor",
        f"{message}: {exc}",
        canonical_changed=False,
        retry_safe=False,
        path="workspace.json",
        recovery=[
            "Restore the complete canonical repository containing its original initial commit"
        ],
    ) from exc
