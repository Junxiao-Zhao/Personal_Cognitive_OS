from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from mem_core.errors import MemError
from mem_core.models import (
    Operation,
    RecordEnvelope,
    latest_by_id,
    proposal_hash,
    transaction_fingerprint,
)
from mem_core.profile import Profile
from mem_core.repository import MemoryRepository
from mem_core.transaction import TransactionManager

from conftest import NOW, make_public_logs_profile


def log_record(
    record_id: str = "log_1", revision: int = 1, message: str = "first"
) -> dict:
    return {
        "id": record_id,
        "revision": revision,
        "recorded_at": NOW,
        "schema_version": "public/log/v1",
        "payload": {"message": message},
    }


def _commit(
    repository: MemoryRepository, message: str, *, no_verify: bool = False
) -> subprocess.CompletedProcess[str]:
    command = ["git", "commit"]
    if no_verify:
        command.append("--no-verify")
    command.extend(["-m", message])
    return subprocess.run(
        command,
        cwd=repository.root,
        text=True,
        capture_output=True,
        check=False,
    )


def _stage_crafted_receipt(
    repository: MemoryRepository,
    operation: Operation,
    *,
    transaction_id: str,
) -> None:
    operations = [operation]
    receipt = {
        "id": transaction_id,
        "transaction_fingerprint": transaction_fingerprint(
            base_commit=repository.head(),
            profile_name=repository.profile.name,
            profile_version=repository.profile.version,
            fingerprint_context={},
            operations=operations,
        ),
        "proposal_hash": proposal_hash(operations),
        "base_commit": repository.head(),
        "profile": f"{repository.profile.name}@{repository.profile.version}",
        "fingerprint_context": {},
        "operation_count": len(operations),
        "operations": [item.normalized() for item in operations],
        "approval_receipt": None,
        "committed_at": NOW,
    }
    with (repository.root / "transactions/transactions.jsonl").open(
        "a", encoding="utf-8", newline="\n"
    ) as handle:
        handle.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n")
    repository._git("add", "transactions/transactions.jsonl")


def test_latest_by_id_keeps_highest_revision() -> None:
    records = [
        {"id": "a", "revision": 1, "payload": {}},
        {"id": "a", "revision": 2, "payload": {}},
        {"id": "b", "revision": 1, "payload": {}},
    ]
    assert latest_by_id(records) == {"a": records[1], "b": records[2]}


def test_record_envelope_defers_id_policy_to_selected_profile(tmp_path: Path) -> None:
    record = log_record(record_id="对象:一")
    assert RecordEnvelope.model_validate(record).id == "对象:一"

    profile_root = make_public_logs_profile(tmp_path / "restricted-profile")
    schema_path = profile_root / "schemas/log.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["id"].update(
        {"pattern": "^log_[A-Za-z0-9]+$", "maxLength": 12}
    )
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    profile = Profile.load(profile_root)

    with pytest.raises(MemError) as exc:
        profile.validate_record_schema("logs", record)
    assert exc.value.detail.code == "SCHEMA_VALIDATION_FAILED"
    assert exc.value.detail.path == "/id"


def test_schema_validator_is_cached_and_reports_invalid_public_record(
    repository: MemoryRepository,
) -> None:
    profile = repository.profile
    assert profile.schema_validator("logs") is profile.schema_validator("logs")

    invalid = log_record()
    invalid["payload"] = {}
    with pytest.raises(MemError) as exc:
        profile.validate_record_schema("logs", invalid)
    assert exc.value.detail.code == "SCHEMA_VALIDATION_FAILED"
    assert exc.value.detail.path == "/payload"


def test_public_log_transaction_commits_to_repository(
    repository: MemoryRepository, tmp_path: Path
) -> None:
    manager = TransactionManager(repository, tmp_path / "state")
    state = manager.begin(transaction_id="txn_public_log", fingerprint_context={"kind": "public-log"})
    manager.append(state.id, Operation(op="append", stream="logs", record=log_record()))

    committed = manager.commit(state.id)

    assert committed["ok"] is True
    assert repository.current_records("logs") == {"log_1": log_record()}
    assert repository.is_clean()


def test_copy_profile_false_external_profile_commits_public_transaction(
    tmp_path: Path,
) -> None:
    profile = Profile.load(make_public_logs_profile(tmp_path / "external-profile"))
    repository = MemoryRepository(tmp_path / "external-memory", profile)
    repository.init(copy_profile=False)
    manager = TransactionManager(repository, tmp_path / "state")
    state = manager.begin(
        transaction_id="txn_external_profile",
        fingerprint_context={"kind": "external-profile"},
    )
    manager.append(state.id, Operation(op="append", stream="logs", record=log_record()))

    committed = manager.commit(state.id)

    marker = json.loads(
        (repository.root / ".mem-profile.json").read_text(encoding="utf-8")
    )
    assert committed["ok"] is True
    assert Path(marker["path"]).is_absolute()
    assert not (repository.root / "profiles" / profile.name).exists()
    assert repository.current_records("logs") == {"log_1": log_record()}
    assert repository.is_clean()


def test_external_profile_mutation_after_init_fails_closed_before_policy_can_weaken(
    tmp_path: Path,
) -> None:
    profile_root = make_public_logs_profile(tmp_path / "external-profile")
    profile = Profile.load(profile_root)
    repository = MemoryRepository(tmp_path / "external-memory", profile)
    repository.init(copy_profile=False)
    log_path = repository.root / "raw/logs.jsonl"
    original_line = json.dumps(log_record(), ensure_ascii=False, sort_keys=True) + "\n"
    log_path.write_text(original_line, encoding="utf-8")
    repository._git("add", "raw/logs.jsonl")
    seeded = _commit(repository, "seed history", no_verify=True)
    assert seeded.returncode == 0

    profile_path = profile_root / "profile.yaml"
    weakened = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    weakened = deepcopy(weakened)
    del weakened["streams"]["logs"]
    weakened["artifact_roots"] = ["raw"]
    profile_path.write_text(yaml.safe_dump(weakened, sort_keys=False), encoding="utf-8")

    tampered_line = original_line.replace('"message": "first"', '"message": "later"')
    assert tampered_line != original_line
    log_path.write_text(tampered_line, encoding="utf-8")
    repository._git("add", "raw/logs.jsonl")
    _stage_crafted_receipt(
        repository,
        Operation(
            op="write_artifact",
            path="raw/logs.jsonl",
            content=tampered_line,
        ),
        transaction_id="txn_external_profile_weakened",
    )

    result = _commit(repository, "tamper with weakened external profile")

    assert result.returncode != 0
    assert "PROFILE_TREE_MISMATCH" in result.stdout + result.stderr


def test_incremental_validation_reports_public_log_delta(
    repository: MemoryRepository, tmp_path: Path
) -> None:
    manager = TransactionManager(repository, tmp_path / "state")
    state = manager.begin(transaction_id="txn_incremental", fingerprint_context={"kind": "incremental"})
    manager.append(state.id, Operation(op="append", stream="logs", record=log_record()))

    validation = manager.validate(state.id)

    assert validation["mode"] == "incremental"
    assert validation["delta"] == {"logs": 1}
    assert validation["records"] == 1


def test_incremental_validation_rejects_duplicate_log_revision(
    repository: MemoryRepository, tmp_path: Path
) -> None:
    manager = TransactionManager(repository, tmp_path / "state")
    first = manager.begin(transaction_id="txn_first", fingerprint_context={"kind": "revision"})
    manager.append(first.id, Operation(op="append", stream="logs", record=log_record()))
    manager.commit(first.id)

    duplicate = manager.begin(transaction_id="txn_duplicate", fingerprint_context={"kind": "revision"})
    manager.append(duplicate.id, Operation(op="append", stream="logs", record=log_record()))

    with pytest.raises(MemError) as exc:
        manager.validate(duplicate.id)
    assert exc.value.detail.code == "REVISION_SEQUENCE_INVALID"


def test_pre_commit_hook_rejects_historical_log_edit(
    repository: MemoryRepository, tmp_path: Path
) -> None:
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
    result = subprocess.run(
        [status["path"]],
        cwd=repository.root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert status["installed"] is True
    assert result.returncode == 1
    assert "APPEND_ONLY_VIOLATION" in result.stdout


def test_repository_worktree_uses_requested_base_commit(
    repository: MemoryRepository, tmp_path: Path
) -> None:
    base_commit = repository.head()
    worktree = tmp_path / "transaction-worktree"

    repository.add_worktree(worktree, base_commit)

    assert (worktree / ".git").exists()
    assert repository._git("rev-parse", "HEAD", cwd=worktree) == base_commit

    repository.remove_worktree(worktree)
    assert not worktree.exists()
