from __future__ import annotations

import json
from pathlib import Path

import pytest

from mem_core import Profile, TransactionManager
from mem_core import hook as mem_hook
from mem_core.delta import is_fast_path, is_messages_only
from mem_core.models import Operation
from mem_core.repository import MemoryRepository
from pco.paths import bundled_profile
from mem_core.registry import default_registry


NOW = "2026-08-16T10:00:00+00:00"


def _logs_profile(root: Path, *, mode: str = "delta_only", run_cross_validators: bool = False) -> Path:
    schema_root = root / "schemas"
    schema_root.mkdir(parents=True)
    (schema_root / "log.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["id", "revision", "recorded_at", "schema_version", "payload"],
                "properties": {
                    "id": {"type": "string"},
                    "revision": {"type": "integer", "minimum": 1},
                    "recorded_at": {"type": "string", "format": "date-time"},
                    "schema_version": {"const": "test/log/v1"},
                    "payload": {"type": "object"},
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "profile.yaml").write_text(
        "\n".join(
            [
                "name: logs-test",
                "version: 1.0.0",
                "streams:",
                "  logs:",
                "    path: raw/logs.jsonl",
                "    schema: schemas/log.schema.json",
                "    schema_version: test/log/v1",
                "    write_policy: auto",
                "    validation:",
                f"      transaction_mode: {mode}",
                f"      run_cross_validators: {'true' if run_cross_validators else 'false'}",
                "artifact_roots: []",
                "validators: []",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def _log_record(record_id: str = "log_1", revision: int = 1) -> dict:
    return {
        "id": record_id,
        "revision": revision,
        "recorded_at": NOW,
        "schema_version": "test/log/v1",
        "payload": {"message": "profile-driven fast path"},
    }


def _stage_transaction(repository: MemoryRepository, state) -> None:
    root = repository.root
    operation = state.operations[0]
    stream_path = repository.profile.stream_path(root, operation.stream or "")
    stream_path.parent.mkdir(parents=True, exist_ok=True)
    with stream_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(operation.record, sort_keys=True) + "\n")
    receipt = {
        "id": state.id,
        "transaction_fingerprint": state.transaction_fingerprint,
        "proposal_hash": state.proposal_hash,
        "base_commit": state.base_commit,
        "profile": f"{state.profile_name}@{state.profile_version}",
        "fingerprint_context": state.fingerprint_context,
        "operation_count": len(state.operations),
        "operations": [item.normalized() for item in state.operations],
        "approval_receipt": None,
        "committed_at": NOW,
    }
    transaction_path = root / "transactions" / "transactions.jsonl"
    with transaction_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, sort_keys=True) + "\n")
    repository._git("add", ".")


def test_bundled_profiles_declare_independent_validation_policies() -> None:
    pco = Profile.load(bundled_profile("pco"), default_registry())
    research = Profile.load(bundled_profile("research"), default_registry())

    assert pco.uses_delta_fast_path("messages")
    assert research.uses_delta_fast_path("observations")
    assert not research.uses_delta_fast_path("claims")


def test_logs_stream_uses_profile_fast_path_for_transaction_and_hook(tmp_path: Path, monkeypatch) -> None:
    profile = Profile.load(_logs_profile(tmp_path / "profile"))
    repository = MemoryRepository(tmp_path / "memory", profile)
    repository.init()
    manager = TransactionManager(repository, tmp_path / "state")
    state = manager.begin(transaction_id="txn_logs", fingerprint_context={"kind": "logs"})
    operation = Operation(op="append", stream="logs", record=_log_record())
    manager.append(state.id, operation)

    assert is_fast_path(profile, [operation])
    assert is_messages_only(profile, [operation])
    assert manager.validate(state.id)["mode"] == "incremental"

    _stage_transaction(repository, manager.load(state.id))

    def fail_structured(*_args, **_kwargs):
        raise AssertionError("structured validation must not run for a policy fast path")

    monkeypatch.setattr(mem_hook, "validate_structured_delta", fail_structured)
    result = mem_hook.validate_repository(repository.root)
    assert result["mode"] == "incremental"
    assert result["increment"]["fast_path"] is True
    assert result["increment"]["delta_by_stream"]["logs"] == [_log_record()]


def test_stream_without_fast_path_policy_keeps_structured_mode(tmp_path: Path) -> None:
    profile = Profile.load(_logs_profile(tmp_path / "profile", mode="structured"))
    operation = Operation(op="append", stream="logs", record=_log_record())

    assert not profile.uses_delta_fast_path("logs")
    assert not is_fast_path(profile, [operation])


def test_invalid_validation_mode_is_rejected_by_profile_load(tmp_path: Path) -> None:
    profile_root = _logs_profile(tmp_path / "profile", mode="not-a-mode")
    with pytest.raises(Exception):
        Profile.load(profile_root)
