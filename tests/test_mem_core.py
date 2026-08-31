from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mem_core.errors import MemError
from mem_core.models import Operation, latest_by_id
from mem_core.repository import MemoryRepository
from mem_core.transaction import TransactionManager

from conftest import NOW


def log_record(record_id: str = "log_1", revision: int = 1, message: str = "first") -> dict:
    return {
        "id": record_id,
        "revision": revision,
        "recorded_at": NOW,
        "schema_version": "public/log/v1",
        "payload": {"message": message},
    }


def test_latest_by_id_keeps_highest_revision() -> None:
    records = [
        {"id": "a", "revision": 1, "payload": {}},
        {"id": "a", "revision": 2, "payload": {}},
        {"id": "b", "revision": 1, "payload": {}},
    ]
    assert latest_by_id(records) == {"a": records[1], "b": records[2]}


def test_schema_validator_is_cached_and_reports_invalid_public_record(repository: MemoryRepository) -> None:
    profile = repository.profile
    assert profile.schema_validator("logs") is profile.schema_validator("logs")

    invalid = log_record()
    invalid["payload"] = {}
    with pytest.raises(MemError) as exc:
        profile.validate_record_schema("logs", invalid)
    assert exc.value.detail.code == "SCHEMA_VALIDATION_FAILED"
    assert exc.value.detail.path == "/payload"


def test_public_log_transaction_commits_to_repository(repository: MemoryRepository, tmp_path: Path) -> None:
    manager = TransactionManager(repository, tmp_path / "state")
    state = manager.begin(transaction_id="txn_public_log", fingerprint_context={"kind": "public-log"})
    manager.append(state.id, Operation(op="append", stream="logs", record=log_record()))

    committed = manager.commit(state.id)

    assert committed["ok"] is True
    assert repository.current_records("logs") == {"log_1": log_record()}
    assert repository.is_clean()


def test_incremental_validation_reports_public_log_delta(repository: MemoryRepository, tmp_path: Path) -> None:
    manager = TransactionManager(repository, tmp_path / "state")
    state = manager.begin(transaction_id="txn_incremental", fingerprint_context={"kind": "incremental"})
    manager.append(state.id, Operation(op="append", stream="logs", record=log_record()))

    validation = manager.validate(state.id)

    assert validation["mode"] == "incremental"
    assert validation["delta"] == {"logs": 1}
    assert validation["records"] == 1


def test_incremental_validation_rejects_duplicate_log_revision(repository: MemoryRepository, tmp_path: Path) -> None:
    manager = TransactionManager(repository, tmp_path / "state")
    first = manager.begin(transaction_id="txn_first", fingerprint_context={"kind": "revision"})
    manager.append(first.id, Operation(op="append", stream="logs", record=log_record()))
    manager.commit(first.id)

    duplicate = manager.begin(transaction_id="txn_duplicate", fingerprint_context={"kind": "revision"})
    manager.append(duplicate.id, Operation(op="append", stream="logs", record=log_record()))

    with pytest.raises(MemError) as exc:
        manager.validate(duplicate.id)
    assert exc.value.detail.code == "REVISION_SEQUENCE_INVALID"


def test_pre_commit_hook_rejects_historical_log_edit(repository: MemoryRepository, tmp_path: Path) -> None:
    manager = TransactionManager(repository, tmp_path / "state")
    state = manager.begin(transaction_id="txn_hook", fingerprint_context={"kind": "append-only"})
    manager.append(state.id, Operation(op="append", stream="logs", record=log_record()))
    manager.commit(state.id)

    log_path = repository.profile.stream_path(repository.root, "logs")
    original = log_path.read_text(encoding="utf-8")
    log_path.write_text(original.replace('"message": "first"', '"message": "changed"'), encoding="utf-8")
    repository._git("add", str(log_path.relative_to(repository.root)))
    log_path.write_text(original, encoding="utf-8")
    status = repository.pre_commit_hook_status()
    result = subprocess.run([status["path"]], cwd=repository.root, text=True, capture_output=True, check=False)

    assert status["installed"] is True
    assert result.returncode == 1
    assert "APPEND_ONLY_VIOLATION" in result.stdout


def test_repository_worktree_uses_requested_base_commit(repository: MemoryRepository, tmp_path: Path) -> None:
    base_commit = repository.head()
    worktree = tmp_path / "transaction-worktree"

    repository.add_worktree(worktree, base_commit)

    assert (worktree / ".git").exists()
    assert repository._git("rev-parse", "HEAD", cwd=worktree) == base_commit

    repository.remove_worktree(worktree)
    assert not worktree.exists()
