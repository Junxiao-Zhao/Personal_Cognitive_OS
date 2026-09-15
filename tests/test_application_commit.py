from __future__ import annotations

import json
from copy import deepcopy
import os
from pathlib import Path
import shutil
from typing import Any, Mapping

import pytest

from mem_core.application import (
    ApplicationCommitRequest,
    application_identity_view,
    application_transaction_id,
)
from mem_core.errors import MemError
from mem_core.repository import MemoryRepository
from mem_core.transaction import TransactionManager


def read_transaction_lines(root: Path, commit: str) -> list[dict[str, Any]]:
    raw = MemoryRepository._git(  # type: ignore[misc]
        object.__new__(MemoryRepository),
        "show",
        f"{commit}:transactions/transactions.jsonl",
        cwd=root,
    )
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def git_worktree_paths(repository: MemoryRepository) -> list[str]:
    return [
        line.removeprefix("worktree ")
        for line in repository._git("worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]


def count_transaction_digest(repository: MemoryRepository, digest: str) -> int:
    return sum(
        record.get("transaction_fingerprint") == digest
        for record in read_transaction_lines(repository.root, repository.head())
    )


class InjectedCrash(RuntimeError):
    pass


@pytest.fixture
def pco_repository(v05_repository: MemoryRepository) -> MemoryRepository:
    return v05_repository


@pytest.fixture
def generic_repository(repository: MemoryRepository) -> MemoryRepository:
    return repository


@pytest.fixture
def raw_application_snapshot(pco_repository: MemoryRepository) -> dict[str, Any]:
    return {
        "policy": "pco_proposal",
        "transaction_digest": "sha256:" + "a" * 64,
        "base_commit": pco_repository.head(),
        "operations": [
            {
                "op": "append",
                "stream": "session_handoffs",
                "record": {
                    "id": "handoff_application_1",
                    "revision": 1,
                    "recorded_at": "2026-08-31T10:00:01+00:00",
                    "schema_version": "pco/session-handoff/v1",
                    "commit_ref": "commit_application_1",
                    "payload": {
                        "previous_revision": None,
                        "change_summary": "Create application handoff",
                        "body_markdown": "A canonical application handoff.",
                        "topics": ["Application commits"],
                        "unanswered_questions": [],
                        "active_tensions": [],
                        "recent_decisions": ["Use a one-shot commit"],
                        "next_directions": ["Continue from canonical memory"],
                        "status": "active",
                    },
                },
            }
        ],
        "receipt": {
            "type": "proposal_commit_receipt",
            "receipt_id": "commit_application_1",
            "review_id": "review_application_1",
            "committed_at": "2026-08-31T10:00:00+00:00",
        },
    }


def request_from_raw_snapshot(
    snapshot: Mapping[str, Any], *, committed_at: str, recorded_at: str
) -> ApplicationCommitRequest:
    receipt = deepcopy(snapshot["receipt"])
    receipt["committed_at"] = committed_at
    operations = deepcopy(snapshot["operations"])
    for operation in operations:
        if operation["op"] == "append":
            operation["record"]["recorded_at"] = recorded_at
    return ApplicationCommitRequest(
        policy=snapshot["policy"],
        transaction_digest=snapshot["transaction_digest"],
        base_commit=snapshot["base_commit"],
        operations=operations,
        receipt=receipt,
    )


@pytest.fixture
def application_request(
    raw_application_snapshot: Mapping[str, Any],
) -> ApplicationCommitRequest:
    return request_from_raw_snapshot(
        raw_application_snapshot,
        committed_at="2026-08-31T10:00:00+00:00",
        recorded_at="2026-08-31T10:00:01+00:00",
    )


@pytest.fixture
def unrelated_application_request(
    raw_application_snapshot: Mapping[str, Any],
) -> ApplicationCommitRequest:
    snapshot = deepcopy(raw_application_snapshot)
    snapshot["transaction_digest"] = "sha256:" + "b" * 64
    snapshot["operations"][0]["record"]["id"] = "handoff_application_2"
    snapshot["operations"][0]["record"]["commit_ref"] = "commit_application_2"
    snapshot["receipt"]["receipt_id"] = "commit_application_2"
    snapshot["receipt"]["review_id"] = "review_application_2"
    return request_from_raw_snapshot(
        snapshot,
        committed_at="2026-08-31T10:30:00+00:00",
        recorded_at="2026-08-31T10:30:01+00:00",
    )


def test_public_mem_write_is_forbidden_for_pco_profile(
    pco_repository: MemoryRepository, tmp_path: Path
) -> None:
    manager = TransactionManager(pco_repository, tmp_path / "state")
    with pytest.raises(MemError) as exc:
        manager.begin(fingerprint_context={"caller": "mem"})
    assert exc.value.detail.code == "DIRECT_PCO_STREAM_WRITE_FORBIDDEN"


def test_application_commit_reuses_transaction_digest(
    pco_repository: MemoryRepository, application_request: ApplicationCommitRequest
) -> None:
    manager = TransactionManager(pco_repository, pco_repository.root.parent / "tmp")
    first = manager.commit_application(application_request)
    second = manager.commit_application(application_request)
    assert first["commit"] == second["commit"]
    assert second["idempotent"] is True


def test_uncommitted_matching_ledger_line_does_not_report_idempotent_success(
    pco_repository: MemoryRepository, application_request: ApplicationCommitRequest
) -> None:
    transaction_record = {
        "id": application_transaction_id(application_request.transaction_digest),
        "application_policy": application_request.policy,
        "base_commit": application_request.base_commit,
        "operation_count": len(application_request.operations),
        "operations": [
            operation.normalized() for operation in application_request.operations
        ],
        "transaction_fingerprint": application_request.transaction_digest,
        "receipt": application_request.receipt,
    }
    txn_log = pco_repository.root / "transactions" / "transactions.jsonl"
    with txn_log.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(transaction_record, ensure_ascii=False, sort_keys=True) + "\n"
        )

    assert (
        count_transaction_digest(pco_repository, application_request.transaction_digest)
        == 0
    )
    with pytest.raises(MemError) as exc:
        TransactionManager(
            pco_repository, pco_repository.root.parent / "tmp"
        ).commit_application(application_request)
    assert exc.value.detail.code == "CANONICAL_DIRTY"


def test_manager_construction_has_no_filesystem_side_effect(
    pco_repository: MemoryRepository, tmp_path: Path
) -> None:
    state_root = tmp_path / "not-created"
    TransactionManager(pco_repository, state_root)
    assert not state_root.exists()


def test_generic_begin_creates_transaction_state_lazily(
    generic_repository: MemoryRepository, tmp_path: Path
) -> None:
    state_root = tmp_path / "lazy-state"
    manager = TransactionManager(generic_repository, state_root)
    assert not state_root.exists()
    state = manager.begin(fingerprint_context={"test": "lazy"})
    assert (state_root / "transactions" / state.id / "state.json").is_file()


def test_application_transaction_id_is_derived_from_digest() -> None:
    digest = "sha256:" + "a" * 64
    assert application_transaction_id(digest) == "txn_" + "a" * 64
    assert "transaction_id" not in ApplicationCommitRequest.model_fields


def test_rebuilt_request_with_new_timestamps_is_still_idempotent(
    pco_repository: MemoryRepository, raw_application_snapshot: Mapping[str, Any]
) -> None:
    application_request = request_from_raw_snapshot(
        raw_application_snapshot,
        committed_at="2026-08-31T10:00:00+00:00",
        recorded_at="2026-08-31T10:00:01+00:00",
    )
    manager = TransactionManager(pco_repository, pco_repository.root.parent / "tmp")
    first = manager.commit_application(application_request)
    rebuilt = request_from_raw_snapshot(
        raw_application_snapshot,
        committed_at="2026-08-31T11:00:00+00:00",
        recorded_at="2026-08-31T11:00:01+00:00",
    )
    assert rebuilt.transaction_digest == application_request.transaction_digest
    assert application_transaction_id(
        rebuilt.transaction_digest
    ) == application_transaction_id(application_request.transaction_digest)
    stored = next(
        record
        for record in read_transaction_lines(pco_repository.root, first["commit"])
        if record["transaction_fingerprint"] == application_request.transaction_digest
    )
    assert application_identity_view(stored) == application_identity_view(rebuilt)
    second = manager.commit_application(rebuilt)
    assert second["commit"] == first["commit"]
    assert second["idempotent"] is True


def test_duplicate_with_forged_ledger_id_is_rejected(
    pco_repository: MemoryRepository,
    application_request: ApplicationCommitRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TransactionManager(pco_repository, pco_repository.root.parent / "tmp")
    first = manager.commit_application(application_request)
    stored = next(
        record
        for record in read_transaction_lines(pco_repository.root, first["commit"])
        if record["transaction_fingerprint"] == application_request.transaction_digest
    )
    forged = deepcopy(stored)
    forged["id"] = "txn_" + "f" * 64
    forged["commit"] = first["commit"]

    monkeypatch.setattr(
        manager,
        "_find_committed_fingerprint",
        lambda digest: (
            forged if digest == application_request.transaction_digest else None
        ),
    )
    with pytest.raises(MemError) as exc:
        manager.commit_application(application_request)
    assert exc.value.detail.code == "WORKSPACE_INTEGRITY_ERROR"
    assert exc.value.detail.canonical_changed is False
    assert exc.value.detail.retry_safe is False
    assert exc.value.detail.recovery == [
        "Restore the complete canonical repository from a trusted copy"
    ]


def test_application_commit_writes_no_transaction_state(
    pco_repository: MemoryRepository,
    application_request: ApplicationCommitRequest,
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "application-state"
    state_root.mkdir()
    manager = TransactionManager(pco_repository, state_root)
    before_worktrees = git_worktree_paths(pco_repository)
    result = manager.commit_application(application_request)
    assert result["ok"] is True
    assert git_worktree_paths(pco_repository) == before_worktrees
    assert not list(state_root.rglob("state.json"))
    assert not list(state_root.rglob("*resume*"))
    assert not (state_root / "application-worktrees").exists()


def test_application_commit_rejects_changed_head(
    pco_repository: MemoryRepository, application_request: ApplicationCommitRequest
) -> None:
    request = application_request.model_copy(update={"base_commit": "0" * 40})
    with pytest.raises(MemError) as exc:
        TransactionManager(
            pco_repository, pco_repository.root.parent / "tmp"
        ).commit_application(request)
    assert exc.value.detail.code == "CANONICAL_HEAD_CHANGED"
    assert exc.value.detail.canonical_changed is False
    assert exc.value.detail.retry_safe is False
    assert exc.value.detail.recovery == [
        "Create a new proposal from pco proposal scaffold against the current HEAD"
    ]


def test_receipt_is_in_same_git_commit_as_business_revision(
    pco_repository: MemoryRepository, application_request: ApplicationCommitRequest
) -> None:
    result = TransactionManager(
        pco_repository, pco_repository.root.parent / "tmp"
    ).commit_application(application_request)
    ledger = read_transaction_lines(pco_repository.root, result["commit"])
    assert ledger[-1]["receipt"] == application_request.receipt
    assert (
        ledger[-1]["transaction_fingerprint"] == application_request.transaction_digest
    )
    assert pco_repository._git(
        "show",
        f"{result['commit']}:session_handoffs/session_handoffs.jsonl",
    ).strip()


def test_retry_after_crash_following_fast_forward_is_idempotent(
    pco_repository: MemoryRepository,
    raw_application_snapshot: Mapping[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "application-state"
    state_root.mkdir()
    manager = TransactionManager(pco_repository, state_root)
    before_worktrees = git_worktree_paths(pco_repository)
    first_request = request_from_raw_snapshot(
        raw_application_snapshot,
        committed_at="2026-08-31T11:00:00+00:00",
        recorded_at="2026-08-31T11:00:01+00:00",
    )
    transaction_id = application_transaction_id(first_request.transaction_digest)
    original = pco_repository.fast_forward

    def crash_after_fast_forward(commit: str) -> None:
        original(commit)
        raise InjectedCrash("after-fast-forward")

    monkeypatch.setattr(pco_repository, "fast_forward", crash_after_fast_forward)
    with pytest.raises(InjectedCrash):
        manager.commit_application(first_request)
    monkeypatch.setattr(pco_repository, "fast_forward", original)
    assert manager._application_worktree_path(first_request.transaction_digest).exists()

    rebuilt = request_from_raw_snapshot(
        raw_application_snapshot,
        committed_at="2026-08-31T12:00:00+00:00",
        recorded_at="2026-08-31T12:00:01+00:00",
    )
    assert application_transaction_id(rebuilt.transaction_digest) == transaction_id
    retry_manager = TransactionManager(pco_repository, state_root)
    retried = retry_manager.commit_application(rebuilt)
    assert retried["idempotent"] is True
    assert (
        count_transaction_digest(pco_repository, first_request.transaction_digest) == 1
    )
    assert git_worktree_paths(pco_repository) == before_worktrees
    assert not retry_manager._application_attempt_dir(
        first_request.transaction_digest
    ).exists()
    assert not (state_root / "application-worktrees").exists()


def test_cleanup_failure_reports_committed_canonical_and_safe_retry(
    pco_repository: MemoryRepository,
    application_request: ApplicationCommitRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_commit = pco_repository.head()
    manager = TransactionManager(
        pco_repository, pco_repository.root.parent / "application-state"
    )
    monkeypatch.setattr(
        manager,
        "_remove_exact_worktree_registration",
        lambda _expected: (_ for _ in ()).throw(
            OSError("injected scoped registration cleanup failure")
        ),
    )

    with pytest.raises(MemError) as exc:
        manager.commit_application(application_request)

    assert pco_repository.head() != base_commit
    assert exc.value.detail.code == "CANONICAL_WORKTREE_CLEANUP_FAILED"
    assert exc.value.detail.canonical_changed is True
    assert exc.value.detail.retry_safe is True
    assert exc.value.detail.recovery == ["pco proposal commit review_application_1"]


def test_application_creation_never_calls_legacy_path_based_remove(
    pco_repository: MemoryRepository,
    application_request: ApplicationCommitRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TransactionManager(
        pco_repository, pco_repository.root.parent / "application-state"
    )
    monkeypatch.setattr(
        pco_repository,
        "remove_worktree",
        lambda _path: pytest.fail("legacy path-based worktree removal was called"),
    )

    result = manager.commit_application(application_request)

    assert result["ok"] is True


@pytest.mark.parametrize("unexpected_kind", ["symlinked_attempt", "worktree_file"])
def test_application_creation_rejects_unexpected_attempt_entries_without_canonical_change(
    pco_repository: MemoryRepository,
    application_request: ApplicationCommitRequest,
    tmp_path: Path,
    unexpected_kind: str,
) -> None:
    state_root = tmp_path / "state"
    manager = TransactionManager(pco_repository, state_root)
    attempt = manager._application_attempt_dir(application_request.transaction_digest)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    sentinel = foreign / "sentinel"
    sentinel.write_text("must survive\n", encoding="utf-8")
    if unexpected_kind == "symlinked_attempt":
        attempt.parent.mkdir(parents=True)
        attempt.symlink_to(foreign, target_is_directory=True)
    else:
        attempt.mkdir(parents=True)
        (attempt / "worktree").write_text("unexpected\n", encoding="utf-8")
    before = pco_repository.head()

    with pytest.raises(MemError) as exc:
        manager.commit_application(application_request)

    assert pco_repository.head() == before
    assert exc.value.detail.code == "CANONICAL_WORKTREE_CLEANUP_FAILED"
    assert exc.value.detail.canonical_changed is False
    assert exc.value.detail.retry_safe is True
    assert sentinel.read_text(encoding="utf-8") == "must survive\n"
    if unexpected_kind == "worktree_file":
        assert (attempt / "worktree").read_text(encoding="utf-8") == "unexpected\n"


def test_public_cleanup_rejects_symlinked_attempt_without_removing_foreign_worktree(
    pco_repository: MemoryRepository,
    application_request: ApplicationCommitRequest,
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    application_root = state_root / "application-worktrees"
    application_root.mkdir(parents=True)
    manager = TransactionManager(pco_repository, state_root)
    attempt = manager._application_attempt_dir(application_request.transaction_digest)
    foreign_attempt = tmp_path / "foreign-attempt"
    foreign_worktree = foreign_attempt / "worktree"
    pco_repository.add_worktree(foreign_worktree, pco_repository.head())
    sentinel = foreign_worktree / "UNOWNED_SENTINEL"
    sentinel.write_text("must survive\n", encoding="utf-8")
    attempt.symlink_to(foreign_attempt, target_is_directory=True)
    before = git_worktree_paths(pco_repository)

    with pytest.raises(MemError) as exc:
        manager.cleanup_application_attempt(
            application_request.transaction_digest,
            review_id=application_request.receipt["review_id"],
        )

    assert exc.value.detail.code == "CANONICAL_WORKTREE_CLEANUP_FAILED"
    assert exc.value.detail.canonical_changed is True
    assert exc.value.detail.retry_safe is True
    assert sentinel.read_text(encoding="utf-8") == "must survive\n"
    assert foreign_worktree.is_dir()
    assert git_worktree_paths(pco_repository) == before


def test_public_cleanup_rejects_symlinked_worktree_without_removing_foreign_registration(
    pco_repository: MemoryRepository,
    application_request: ApplicationCommitRequest,
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    manager = TransactionManager(pco_repository, state_root)
    attempt = manager._application_attempt_dir(application_request.transaction_digest)
    attempt.mkdir(parents=True)
    foreign_worktree = tmp_path / "foreign-worktree"
    pco_repository.add_worktree(foreign_worktree, pco_repository.head())
    (attempt / "worktree").symlink_to(foreign_worktree, target_is_directory=True)
    before = git_worktree_paths(pco_repository)

    with pytest.raises(MemError) as exc:
        manager.cleanup_application_attempt(
            application_request.transaction_digest,
            review_id=application_request.receipt["review_id"],
        )

    assert exc.value.detail.code == "CANONICAL_WORKTREE_CLEANUP_FAILED"
    assert foreign_worktree.is_dir()
    assert git_worktree_paths(pco_repository) == before


def test_public_cleanup_prunes_only_missing_owned_registration_and_preserves_real_worktrees(
    pco_repository: MemoryRepository,
    application_request: ApplicationCommitRequest,
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    manager = TransactionManager(pco_repository, state_root)
    owned = manager._application_worktree_path(application_request.transaction_digest)
    foreign = tmp_path / "foreign-real-worktree"
    pco_repository.add_worktree(owned, pco_repository.head())
    pco_repository.add_worktree(foreign, pco_repository.head())
    foreign_head = pco_repository._git("rev-parse", "HEAD", cwd=foreign)

    manager.cleanup_application_attempt(
        application_request.transaction_digest,
        review_id=application_request.receipt["review_id"],
    )

    assert not owned.exists()
    assert foreign.is_dir()
    assert pco_repository._git("rev-parse", "HEAD", cwd=foreign) == foreign_head
    assert str(foreign.absolute()) in git_worktree_paths(pco_repository)


def test_public_cleanup_preserves_unrelated_prunable_stale_registration_byte_for_byte(
    pco_repository: MemoryRepository,
    application_request: ApplicationCommitRequest,
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    manager = TransactionManager(pco_repository, state_root)
    owned = manager._application_worktree_path(application_request.transaction_digest)
    unrelated = tmp_path / "unrelated-stale-worktree"
    pco_repository.add_worktree(owned, pco_repository.head())
    pco_repository.add_worktree(unrelated, pco_repository.head())
    shutil.rmtree(unrelated)
    unrelated_path = str(unrelated.absolute())
    before_paths = git_worktree_paths(pco_repository)
    assert unrelated_path in before_paths
    git_admin = pco_repository.root / ".git/worktrees"
    before_admin = {
        path.relative_to(git_admin): path.read_bytes()
        for path in git_admin.rglob("*")
        if path.is_file()
    }

    manager.cleanup_application_attempt(
        application_request.transaction_digest,
        review_id=application_request.receipt["review_id"],
    )

    assert unrelated_path in git_worktree_paths(pco_repository)
    after_admin = {
        path.relative_to(git_admin): path.read_bytes()
        for path in git_admin.rglob("*")
        if path.is_file()
    }
    matching_gitdirs = [
        key
        for key, value in before_admin.items()
        if key.name == "gitdir" and unrelated_path.encode("utf-8") in value
    ]
    assert len(matching_gitdirs) == 1
    unrelated_admin = matching_gitdirs[0].parts[0]
    unrelated_keys = {
        key for key in before_admin if key.parts[0] == unrelated_admin
    }
    assert {key: after_admin[key] for key in unrelated_keys} == {
        key: before_admin[key] for key in unrelated_keys
    }


@pytest.mark.parametrize("swap", ["attempt", "worktree"])
def test_public_cleanup_rejects_parent_or_worktree_swap_before_destructive_git_cleanup(
    pco_repository: MemoryRepository,
    application_request: ApplicationCommitRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap: str,
) -> None:
    state_root = tmp_path / "state"
    manager = TransactionManager(pco_repository, state_root)
    attempt = manager._application_attempt_dir(application_request.transaction_digest)
    worktree = attempt / "worktree"
    pco_repository.add_worktree(worktree, pco_repository.head())
    displaced = tmp_path / f"displaced-{swap}"
    foreign_attempt = tmp_path / f"foreign-{swap}"
    foreign_worktree = foreign_attempt / "worktree"
    pco_repository.add_worktree(foreign_worktree, pco_repository.head())
    sentinel = foreign_worktree / "UNOWNED_SENTINEL"
    sentinel.write_text("must survive\n", encoding="utf-8")
    real_detach = TransactionManager._cleanup_rename_noreplace
    swapped = False

    def swap_before_detach(parent_fd, source, target, expected):
        nonlocal swapped
        if not swapped:
            swapped = True
            if swap == "attempt":
                attempt.rename(displaced)
                attempt.symlink_to(foreign_attempt, target_is_directory=True)
            else:
                worktree.rename(displaced)
                worktree.symlink_to(foreign_worktree, target_is_directory=True)
        return real_detach(parent_fd, source, target, expected)

    monkeypatch.setattr(
        TransactionManager,
        "_cleanup_rename_noreplace",
        staticmethod(swap_before_detach),
    )

    with pytest.raises(MemError) as exc:
        manager.cleanup_application_attempt(
            application_request.transaction_digest,
            review_id=application_request.receipt["review_id"],
        )

    assert exc.value.detail.code == "CANONICAL_WORKTREE_CLEANUP_FAILED"
    assert sentinel.read_text(encoding="utf-8") == "must survive\n"
    assert displaced.is_dir()
    assert foreign_worktree.is_dir()


def test_unrelated_head_advance_without_digest_is_stale(
    pco_repository: MemoryRepository,
    application_request: ApplicationCommitRequest,
    unrelated_application_request: ApplicationCommitRequest,
) -> None:
    TransactionManager(
        pco_repository, pco_repository.root.parent / "tmp"
    ).commit_application(unrelated_application_request)
    with pytest.raises(MemError) as exc:
        TransactionManager(
            pco_repository, pco_repository.root.parent / "tmp"
        ).commit_application(application_request)
    assert exc.value.detail.code == "CANONICAL_HEAD_CHANGED"
