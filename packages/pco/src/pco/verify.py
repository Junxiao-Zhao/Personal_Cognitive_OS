"""Read-only integrity verification for a dedicated PCO workspace.

Projection verification intentionally is not implemented here.  The current
projection format is owned by the derivation plan; this module verifies only
the workspace state that exists independently of that future format.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mem_core.errors import MemError, ensure
from mem_core.repository import MemoryRepository
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import __version__
from .config import PROPOSAL_MANIFEST_VERSION
from .skill_resources import inspect_skill_manifest, verify_skill_tree
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

    model_config = ConfigDict(extra="allow")

    workspace_id: str = Field(pattern=r"^workspace_[0-9a-f]{32}$")
    workspace_format_version: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)


def load_workspace_identity(repository: MemoryRepository) -> WorkspaceIdentity:
    """Read and anchor workspace identity to its exact initial Git blob."""

    root_commit = _sole_root_commit(repository)
    current_path = repository.root / "workspace.json"
    try:
        current_bytes = current_path.read_bytes()
        current = WorkspaceIdentity.model_validate_json(current_bytes)
        root_bytes = _root_blob_bytes(repository, root_commit, "workspace.json")
        original = WorkspaceIdentity.model_validate_json(root_bytes)
    except (OSError, UnicodeDecodeError, ValidationError, RuntimeError) as exc:
        _integrity_error("Unable to read a valid root-anchored workspace identity", exc)

    ensure(
        current == original and current_bytes == root_bytes,
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

    result = _verify_workspace(workspace, hash_skill_payloads=True)
    if path is not None:
        _verify_requested_path(workspace, path)
    return result


def doctor_workspace(workspace: "Workspace") -> dict[str, Any]:
    """Check workspace metadata and SKILL compatibility without payload hashing."""
    return _verify_workspace(workspace, hash_skill_payloads=False)


def _verify_workspace(
    workspace: "Workspace", *, hash_skill_payloads: bool
) -> dict[str, Any]:
    identity = load_workspace_identity(workspace.repository)
    skills = _verify_local_skills(workspace, hash_payloads=hash_skill_payloads)
    compatibility = _verify_compatibility(identity, skills)
    return {
        "ok": True,
        "identity": identity.model_dump(mode="json"),
        "compatibility": compatibility,
        "skills": skills,
        "permission_contract": dict(_PERMISSION_CONTRACT),
    }


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
    except json.JSONDecodeError as exc:
        raise MemError(
            _INTEGRITY_CODE,
            "doctor",
            f"Unable to read canonical transaction receipts: {exc}",
            canonical_changed=False,
            retry_safe=False,
            path=f"transactions/transactions.jsonl:{line_number}",
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise MemError(
            _INTEGRITY_CODE,
            "doctor",
            f"Unable to read canonical transaction receipts: {exc}",
            canonical_changed=False,
            retry_safe=False,
            path="transactions/transactions.jsonl",
        ) from exc


def _verify_local_skills(
    workspace: "Workspace", *, hash_payloads: bool
) -> dict[str, dict[str, Any]]:
    verified: dict[str, dict[str, Any]] = {}
    for name in _SKILL_NAMES:
        skill_path = _workspace_skill_path(workspace, name)
        result = (
            verify_skill_tree(skill_path, __version__)
            if hash_payloads
            else inspect_skill_manifest(skill_path, __version__)
        )
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


def _workspace_skill_path(workspace: "Workspace", name: str) -> Path:
    for relative in (".agents", ".agents/skills"):
        candidate = workspace.paths.root / relative
        try:
            observed = candidate.lstat()
        except OSError as exc:
            raise MemError(
                "WORKSPACE_SKILL_INVALID",
                "verify",
                "workspace SKILL ancestor must be a real directory",
                canonical_changed=False,
                retry_safe=False,
                path=relative,
                value=str(exc),
            ) from exc
        ensure(
            not stat.S_ISLNK(observed.st_mode) and stat.S_ISDIR(observed.st_mode),
            "WORKSPACE_SKILL_INVALID",
            "verify",
            "workspace SKILL ancestor must be a real directory",
            canonical_changed=False,
            retry_safe=False,
            path=relative,
        )
    return workspace.paths.skills / name


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


def _sole_root_commit(repository: MemoryRepository) -> str:
    try:
        shallow = repository._git("rev-parse", "--is-shallow-repository")
        roots = repository._git("rev-list", "--max-parents=0", "HEAD").splitlines()
    except MemError as exc:
        _integrity_error("Unable to inspect canonical repository history", exc)
    ensure(
        shallow == "false",
        _INTEGRITY_CODE,
        "doctor",
        "Cannot anchor identity in a shallow canonical repository",
        canonical_changed=False,
        retry_safe=False,
    )
    ensure(
        len(roots) == 1,
        _INTEGRITY_CODE,
        "doctor",
        "Canonical repository must have exactly one reachable root commit",
        canonical_changed=False,
        retry_safe=False,
    )
    return roots[0]


def _root_blob_bytes(repository: MemoryRepository, commit: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "cat-file", "blob", f"{commit}:{path}"],
        cwd=repository.root,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "git cat-file failed")
    return result.stdout


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
