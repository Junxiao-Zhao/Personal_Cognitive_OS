from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mem_core.errors import MemError
from mem_core.transaction import TransactionManager
import pytest

from pco.proposals.models import BusinessFile, LocalFileMaterial, ProposalManifest
from pco.proposals.service import ProposalService
from pco.proposals import archive as archive_module
from pco.proposals import service as service_module


def _write_evidence_proposal(
    workspace,
    proposal_factory,
    business_markdown_factory,
    *,
    review_id: str = "review_commit",
    local_file: Path | None = None,
):
    root = proposal_factory(review_id)
    (root / "evidence").mkdir()
    (root / "evidence/ev_commit.md").write_bytes(
        business_markdown_factory(
            object_type="evidence",
            record_id="ev_commit",
            fields={
                "change_summary": "Record reviewed evidence",
                "speaker": "user",
                "source_kind": "conversation",
                "user_authored": True,
                "fidelity": "selected_excerpt",
                "status": "active",
            },
            body="A reviewed statement.",
        )
    )
    materials = []
    if local_file is not None:
        materials.append(
            LocalFileMaterial(
                material_id="local_context",
                kind="local_file",
                locator=local_file.as_uri(),
                expected_content_hash="sha256:"
                + hashlib.sha256(local_file.read_bytes()).hexdigest(),
                fidelity="full_source",
            )
        )
    manifest = ProposalManifest(
        review_id=review_id,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        input_material=materials,
        files=[BusinessFile(path="evidence/ev_commit.md", operation="create")],
    )
    (root / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root


def _transaction_count(workspace, receipt_id: str) -> int:
    ledger = workspace.repository.root / "transactions/transactions.jsonl"
    return sum(
        json.loads(line).get("receipt", {}).get("receipt_id") == receipt_id
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def test_commit_is_atomic_idempotent_and_uses_no_persistent_transaction_state(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    _write_evidence_proposal(workspace, proposal_factory, business_markdown_factory)
    monkeypatch.setattr(
        TransactionManager,
        "_save",
        lambda *_args, **_kwargs: pytest.fail(
            "application commit persisted TransactionState"
        ),
    )
    before_tmp = sorted(
        path.relative_to(workspace.paths.tmp) for path in workspace.paths.tmp.rglob("*")
    )

    first = ProposalService(workspace).commit("review_commit")
    second = ProposalService(workspace).commit("review_commit")

    assert first["canonical_status"] == second["canonical_status"] == "committed"
    assert first["receipt_id"] == second["receipt_id"]
    assert _transaction_count(workspace, first["receipt_id"]) == 1
    assert (
        sorted(
            path.relative_to(workspace.paths.tmp)
            for path in workspace.paths.tmp.rglob("*")
        )
        == before_tmp
    )


def test_derivation_runs_after_lock_release(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    _write_evidence_proposal(workspace, proposal_factory, business_markdown_factory)

    def derive(ws):
        with ws.repository.lock(blocking=False):
            return {"status": "test-derived"}

    monkeypatch.setattr(service_module, "derive_after_commit", derive)
    result = ProposalService(workspace).commit("review_commit")

    assert result["derivation"] == {"status": "test-derived"}


def test_archive_failure_does_not_block_derivation_or_canonical_success(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    root = _write_evidence_proposal(
        workspace, proposal_factory, business_markdown_factory
    )
    derived = []
    monkeypatch.setattr(
        "pco.proposals.archive.atomic_rename",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected")),
    )
    monkeypatch.setattr(
        service_module,
        "derive_after_commit",
        lambda _workspace: derived.append("ran") or {"ok": False, "error": "injected"},
    )

    result = ProposalService(workspace).commit("review_commit")

    assert result["canonical_status"] == "committed"
    assert result["archive_status"] == "pending"
    assert result["derivation"] == {"ok": False, "error": "injected"}
    assert derived == ["ran"]
    assert root.is_dir()


def test_unexpected_archive_exception_is_warning_and_derivation_still_runs(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    _write_evidence_proposal(workspace, proposal_factory, business_markdown_factory)
    monkeypatch.setattr(
        service_module,
        "archive_committed_proposal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("archive bug")),
    )
    monkeypatch.setattr(
        service_module,
        "derive_after_commit",
        lambda _workspace: {"ok": True, "status": "test-derived"},
    )

    result = ProposalService(workspace).commit("review_commit")

    assert result["canonical_status"] == "committed"
    assert result["archive_status"] == "pending"
    assert result["archive_warning"]["code"] == "PROPOSAL_ARCHIVE_PENDING"
    assert result["derivation"]["status"] == "test-derived"


def test_receipt_retry_precedes_active_discovery_and_never_reads_external_input(
    workspace, proposal_factory, business_markdown_factory, tmp_path, monkeypatch
) -> None:
    local = tmp_path / "external.txt"
    local.write_text("captured once\n", encoding="utf-8")
    _write_evidence_proposal(
        workspace,
        proposal_factory,
        business_markdown_factory,
        review_id="review_retry",
        local_file=local,
    )
    first = ProposalService(workspace).commit("review_retry")
    assert first["canonical_status"] == "committed"
    local.unlink()
    (workspace.paths.proposals / "review_other").symlink_to(tmp_path)
    monkeypatch.setattr(
        "pco.proposals.materials.capture_material",
        lambda *_args, **_kwargs: pytest.fail("archive-only retry read external input"),
    )

    retried = ProposalService(workspace).commit("review_retry")

    assert retried["canonical_status"] == "committed"
    assert retried["receipt_id"] == first["receipt_id"]


def test_commit_rechecks_receipt_inside_the_same_lock_as_capture_and_commit(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    _write_evidence_proposal(workspace, proposal_factory, business_markdown_factory)
    real_lookup = service_module.find_proposal_receipt
    real_capture = service_module.capture_snapshot_from_root
    lock_states = []

    def lock_available() -> bool:
        try:
            with workspace.repository.lock(blocking=False):
                return True
        except MemError:
            return False

    def lookup(*args, **kwargs):
        lock_states.append(lock_available())
        return real_lookup(*args, **kwargs)

    def capture(*args, **kwargs):
        assert lock_available() is False
        return real_capture(*args, **kwargs)

    monkeypatch.setattr(service_module, "find_proposal_receipt", lookup)
    monkeypatch.setattr(service_module, "capture_snapshot_from_root", capture)

    result = ProposalService(workspace).commit("review_commit")

    assert result["canonical_status"] == "committed"
    assert lock_states == [True, False]


def test_commit_calls_application_path_with_lock_held_true(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    _write_evidence_proposal(workspace, proposal_factory, business_markdown_factory)
    real_commit = TransactionManager.commit_application

    def commit(manager, request, *, lock_held=False):
        assert lock_held is True
        with pytest.raises(MemError):
            with workspace.repository.lock(blocking=False):
                pass
        return real_commit(manager, request, lock_held=lock_held)

    monkeypatch.setattr(TransactionManager, "commit_application", commit)

    assert (
        ProposalService(workspace).commit("review_commit")["canonical_status"]
        == "committed"
    )


def test_retry_after_fast_forward_cleanup_crash_finds_receipt_and_does_not_recommit(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    _write_evidence_proposal(workspace, proposal_factory, business_markdown_factory)
    real_cleanup = TransactionManager.cleanup_application_attempt
    calls = 0

    def fail_once(manager, transaction_digest, *, review_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("crash after canonical fast-forward")
        return real_cleanup(manager, transaction_digest, review_id=review_id)

    monkeypatch.setattr(TransactionManager, "cleanup_application_attempt", fail_once)

    with pytest.raises(RuntimeError, match="fast-forward"):
        ProposalService(workspace).commit("review_commit")
    receipt_id = next(
        json.loads(line)["receipt"]["receipt_id"]
        for line in (workspace.repository.root / "transactions/transactions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    )

    retried = ProposalService(workspace).commit("review_commit")

    assert retried["canonical_status"] == "committed"
    assert retried["receipt_id"] == receipt_id
    assert _transaction_count(workspace, receipt_id) == 1
    assert not (workspace.paths.tmp / "application-worktrees").exists()


def test_archive_only_retry_skips_stale_base_validation(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    root = _write_evidence_proposal(
        workspace, proposal_factory, business_markdown_factory
    )
    real_rename = archive_module.atomic_rename
    failures = 1

    def fail_first(*args, **kwargs):
        nonlocal failures
        if failures:
            failures -= 1
            raise OSError("leave active proposal for retry")
        return real_rename(*args, **kwargs)

    monkeypatch.setattr("pco.proposals.archive.atomic_rename", fail_first)
    first = ProposalService(workspace).commit("review_commit")
    assert first["archive_status"] == "pending"
    workspace.repository._git(
        "commit", "--no-verify", "--allow-empty", "-m", "advance canonical HEAD"
    )

    retried = ProposalService(workspace).commit("review_commit")

    assert retried["canonical_status"] == "committed"
    assert retried["archive_status"] == "archived"
    assert not root.exists()


def test_commit_releases_lock_when_source_changes_during_capture(
    workspace, proposal_factory, business_markdown_factory, tmp_path
) -> None:
    local = tmp_path / "external.txt"
    local.write_text("reviewed\n", encoding="utf-8")
    _write_evidence_proposal(
        workspace,
        proposal_factory,
        business_markdown_factory,
        review_id="review_changed",
        local_file=local,
    )
    local.write_text("changed\n", encoding="utf-8")

    with pytest.raises(MemError) as exc:
        ProposalService(workspace).commit("review_changed")

    assert exc.value.detail.code == "SOURCE_CHANGED_DURING_SNAPSHOT"
    with workspace.repository.lock(blocking=False):
        pass


def test_abort_removes_only_one_matching_active_proposal_without_canonical_change(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    root = _write_evidence_proposal(
        workspace,
        proposal_factory,
        business_markdown_factory,
        review_id="review_abort",
    )
    canonical_head = workspace.repository.head()

    result = ProposalService(workspace).abort("review_abort")

    assert result == {"ok": True, "status": "aborted", "review_id": "review_abort"}
    assert not root.exists()
    assert workspace.repository.head() == canonical_head
