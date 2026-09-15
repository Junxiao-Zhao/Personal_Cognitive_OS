"""Proposal lifecycle orchestration around one canonical transaction lock."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

from mem_core.errors import MemError, ensure
from mem_core.models import ApplicationCommitRequest
from mem_core.transaction import TransactionManager

from .archive import ArchiveResult, archive_committed_proposal, remove_active_proposal
from .models import ProposalManifest
from .operations import SemanticNoOp, build_commit_request, find_proposal_receipt
from .paths import ProposalPaths
from .snapshot import capture_snapshot_from_root
from .validation import validate_snapshot

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

    def validate(self, review_id: str) -> dict[str, Any]:
        """Read and validate one active proposal without mutating the workspace."""

        try:
            active = self.paths.active_proposals()
            ensure(
                bool(active),
                "PROPOSAL_NOT_FOUND",
                "proposal_validation",
                "No active proposal exists for the requested review",
                path=str(self.paths.root / review_id),
                canonical_changed=False,
                retry_safe=False,
            )
            ensure(
                len(active) == 1 and active[0].name == review_id,
                "PROPOSAL_MANIFEST_MISMATCH",
                "proposal_validation",
                "The requested review is not the single active proposal",
                path=str(self.paths.root / review_id),
                canonical_changed=False,
                retry_safe=False,
            )
            snapshot = capture_snapshot_from_root(active[0], include_local=True)
            return validate_snapshot(
                snapshot, self.workspace, require_hashes=False
            ).as_dict()
        except MemError as exc:
            return exc.as_dict()

    def _require_active(
        self, review_id: str, *, operation: str = "proposal_commit"
    ) -> Any:
        active = self.paths.active_proposals()
        ensure(
            len(active) == 1 and active[0].name == review_id,
            "PROPOSAL_MANIFEST_MISMATCH",
            operation,
            "The requested review is not the single active proposal",
            path=str(self.paths.root / review_id),
            canonical_changed=False,
            retry_safe=False,
            recovery=["Keep exactly one matching active review directory"],
        )
        return active[0]

    def _finish_committed(
        self,
        receipt: dict[str, Any],
        archive: ArchiveResult,
        *,
        canonical: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        derivation = _run_derivation(self.workspace)
        result: dict[str, Any] = {
            "ok": True,
            "canonical_status": "committed",
            "receipt_id": receipt["receipt_id"],
            "transaction_digest": receipt["transaction_digest"],
            **archive.as_dict(),
            "derivation": derivation,
        }
        if canonical is not None:
            result["canonical"] = canonical
        return result

    def commit(self, review_id: str) -> dict[str, Any]:
        """Commit once, then independently archive and derive after lock release."""

        manager = TransactionManager(
            self.workspace.repository, self.workspace.paths.tmp
        )
        existing = find_proposal_receipt(self.workspace, review_id)
        if existing is not None:
            manager.cleanup_application_attempt(
                existing["transaction_digest"], review_id=review_id
            )
            archive = _run_archive(self.workspace, existing)
            return self._finish_committed(existing, archive)

        self._require_active(review_id)
        canonical: dict[str, Any] | None = None
        receipt: dict[str, Any] | None = None
        with self.workspace.repository.lock():
            existing = find_proposal_receipt(self.workspace, review_id)
            if existing is not None:
                receipt = existing
            else:
                active = self._require_active(review_id)
                snapshot = capture_snapshot_from_root(active, include_local=True)
                validated = validate_snapshot(
                    snapshot, self.workspace, require_hashes=True
                )
                request = build_commit_request(self.workspace, snapshot, validated)
                if isinstance(request, SemanticNoOp):
                    return request.as_result()
                assert isinstance(request, ApplicationCommitRequest)
                canonical = manager.commit_application(request, lock_held=True)
                receipt = request.receipt
        assert receipt is not None
        if existing is not None:
            manager.cleanup_application_attempt(
                receipt["transaction_digest"], review_id=review_id
            )
        archive = _run_archive(self.workspace, receipt)
        return self._finish_committed(receipt, archive, canonical=canonical)

    def abort(self, review_id: str) -> dict[str, Any]:
        """Delete only the single matching active proposal, never canonical state."""

        try:
            return remove_active_proposal(self.workspace.paths, review_id)
        except MemError:
            raise
        except (OSError, TypeError, ValueError, UnicodeError) as exc:
            raise MemError(
                "PROPOSAL_PATH_UNSAFE",
                "proposal_abort",
                "Active proposal changed or contains an unsafe entry",
                path=str(self.paths.root / review_id),
                value=str(exc),
                recovery=["Inspect and restore one closed, real active proposal tree"],
                canonical_changed=False,
                retry_safe=False,
            ) from exc


def derive_after_commit(workspace: "Workspace") -> dict[str, Any]:
    """Invoke derivation lazily after the canonical transaction lock is released."""

    from ..derivation import derive_all

    return derive_all(workspace)


def _run_derivation(workspace: "Workspace") -> dict[str, Any]:
    try:
        result = derive_after_commit(workspace)
        return (
            result
            if isinstance(result, dict)
            else {
                "ok": False,
                "status": "failed",
                "error": "invalid derivation result",
            }
        )
    except MemError as exc:
        return exc.as_dict()
    except Exception as exc:  # canonical success is never rolled back by derivation
        return {
            "ok": False,
            "error": {
                "code": "DERIVATION_FAILED",
                "phase": "derivation",
                "message": "Post-commit derivation failed independently",
                "canonical_changed": False,
                "retry_safe": True,
                "recovery": ["Run pco derive all again"],
            },
            "diagnostic": {"value": str(exc)},
        }


def _run_archive(workspace: "Workspace", receipt: dict[str, Any]) -> ArchiveResult:
    try:
        return archive_committed_proposal(workspace.paths, receipt)
    except (
        Exception
    ) as exc:  # canonical success must survive non-canonical archive failure
        active = workspace.paths.proposals / receipt["review_id"]
        committed = workspace.paths.proposals / ".committed" / receipt["receipt_id"]
        return ArchiveResult(
            "pending",
            str(active),
            str(committed),
            {
                "code": "PROPOSAL_ARCHIVE_PENDING",
                "message": f"Proposal archive failed independently: {exc}",
                "suggested_action": f"Rerun pco proposal commit {receipt['review_id']}",
            },
        )
