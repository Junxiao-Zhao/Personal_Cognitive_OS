from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from mem_core.errors import MemError
from mem_core.models import Operation
from mem_core.profile import Profile
from mem_core.registry import default_registry
from mem_core.repository import MemoryRepository
from mem_core.transaction import TransactionManager
from pco.paths import bundled_profile

from conftest import NOW, envelope, event, hypothesis, meta


def test_protected_stream_requires_exact_approval(workspace) -> None:
    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    transaction = manager.begin(transaction_id="txn_meta", fingerprint_context={"checkpoint_id": "ckpt_1"})
    message = envelope(
        "msg_user_1",
        "conversation-message/v1",
        {
            "thread_id": workspace.thread().thread_id,
            "epoch_id": workspace.thread().active_epoch_id,
            "harness": "fake",
            "native_session_id": "ses_fake",
            "native_message_id": "native_user",
            "role": "user",
            "kind": "conversation",
            "content": "这是用户证据。",
            "reasoning": None,
            "refs": [],
            "created_at": NOW,
        },
    )
    meta_record = meta()
    meta_record["payload"]["promotion_refs"] = []
    manager.append(transaction.id, Operation(op="append", stream="messages", record=message))
    manager.append(transaction.id, Operation(op="append", stream="meta_revisions", record=meta_record))

    validation = manager.validate(transaction.id)
    assert validation["approval_required"] is True
    with pytest.raises(MemError) as error:
        manager.commit(transaction.id)
    assert error.value.detail.code == "USER_APPROVAL_REQUIRED"

    proposal_hash = manager.load(transaction.id).proposal_hash
    manager.attach_approval(
        transaction.id,
        checkpoint_id="ckpt_1",
        proposal_hash_value=proposal_hash,
        receipt_id="approval_pending",
    )
    committed = manager.commit(transaction.id)
    assert committed["ok"]
    assert workspace.repository.current_records("meta_revisions")["meta_current"]["revision"] == 1
    assert manager.commit(transaction.id)["idempotent"] is True


def test_profile_rejects_assistant_as_user_evidence(workspace) -> None:
    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    raw = manager.begin(transaction_id="txn_assistant", fingerprint_context={"kind": "raw"})
    assistant = envelope(
        "msg_assistant",
        "conversation-message/v1",
        {
            "thread_id": workspace.thread().thread_id,
            "epoch_id": workspace.thread().active_epoch_id,
            "harness": "fake",
            "native_session_id": "ses_fake",
            "native_message_id": "native_assistant",
            "role": "assistant",
            "kind": "conversation",
            "content": "我认为用户害怕失败。",
            "reasoning": None,
            "refs": [],
            "created_at": NOW,
        },
    )
    manager.append(raw.id, Operation(op="append", stream="messages", record=assistant))
    manager.commit(raw.id)

    transaction = manager.begin(transaction_id="txn_bad_evidence", fingerprint_context={"kind": "checkpoint"})
    bad_hypothesis = envelope(
        "hyp_bad",
        "pco/hypothesis/v1",
        {
            "statement": "用户害怕失败。",
            "confidence": "low",
            "evidence_refs": ["message:msg_assistant"],
            "counter_evidence_refs": [],
            "status": "hypothesis",
            "policy_version": "promotion@0.3",
        },
    )
    manager.append(transaction.id, Operation(op="append", stream="hypotheses", record=bad_hypothesis))
    with pytest.raises(MemError) as error:
        manager.validate(transaction.id)
    assert error.value.detail.code == "EVIDENCE_INELIGIBLE"
    assert error.value.detail.path == "/payload/evidence_refs/0"


def test_unknown_and_missing_structured_evidence_are_rejected(workspace) -> None:
    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    for reference in ("evt_missing", "unknown:whatever"):
        record = hypothesis()
        record["payload"]["evidence_refs"] = [reference]
        state = manager.begin(fingerprint_context={"case": reference})
        manager.append(state.id, Operation(op="append", stream="hypotheses", record=record))
        with pytest.raises(MemError) as error:
            manager.validate(state.id)
        assert error.value.detail.code == "EVIDENCE_REFERENCE_INVALID"


def test_entity_ids_reject_path_traversal(workspace) -> None:
    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    record = event()
    record["id"] = "../../state/context/current"
    state = manager.begin(fingerprint_context={"case": "unsafe-id"})
    manager.append(state.id, Operation(op="append", stream="events", record=record))
    with pytest.raises(MemError) as error:
        manager.validate(state.id)
    assert error.value.detail.code == "ENVELOPE_INVALID"


def test_non_pco_profile_uses_same_core_without_code_changes(tmp_path: Path) -> None:
    profile = Profile.load(bundled_profile("research"), default_registry())
    repository = MemoryRepository(tmp_path / "research-memory", profile)
    repository.init()
    manager = TransactionManager(repository, tmp_path / "research-state")
    transaction = manager.begin(transaction_id="txn_observation", fingerprint_context={"run": "r1"})
    observation = envelope(
        "obs_1",
        "research/observation/v1",
        {"text": "The sample changed color.", "source": "lab-note-1"},
    )
    manager.append(transaction.id, Operation(op="append", stream="observations", record=observation))
    assert manager.commit(transaction.id)["ok"]
    result = profile.invoke("retrieval.search", repo_root=repository.root, query="changed color")
    assert result["results"][0]["id"] == "obs_1"
    projection = profile.invoke("projections.markdown", repo_root=repository.root, output_root=tmp_path / "research-projection")
    assert projection["ok"] and projection["pages"] == 1

    forbidden = manager.begin(transaction_id="txn_read_only", fingerprint_context={"run": "r2"})
    manager.append(
        forbidden.id,
        Operation(
            op="append",
            stream="papers",
            record=envelope("paper_1", "research/paper/v1", {"title": "Read-only evidence"}),
        ),
    )
    with pytest.raises(MemError) as error:
        manager.validate(forbidden.id)
    assert error.value.detail.code == "STREAM_READ_ONLY"


def test_pre_commit_hook_reuses_profile_validation(workspace) -> None:
    status = workspace.repository.pre_commit_hook_status()
    assert status["installed"]
    messages = workspace.config.memory_root / "raw" / "conversations" / "messages.jsonl"
    original = messages.read_text(encoding="utf-8")
    messages.write_text('{"not":"a valid envelope"}\n', encoding="utf-8")
    workspace.repository._git("add", str(messages.relative_to(workspace.config.memory_root)))
    messages.write_text(original, encoding="utf-8")
    result = subprocess.run(
        [status["path"]],
        cwd=workspace.config.memory_root,
        text=True,
        capture_output=True,
        check=False,
        env={
            **__import__("os").environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
    )
    assert result.returncode == 1
    assert "ENVELOPE_INVALID" in result.stdout


def test_pre_commit_hook_rejects_schema_valid_historical_edit(workspace) -> None:
    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    transaction = manager.begin(transaction_id="txn_raw_for_hook", fingerprint_context={"kind": "raw"})
    message = envelope(
        "msg_hook_user",
        "conversation-message/v1",
        {
            "thread_id": workspace.thread().thread_id,
            "epoch_id": workspace.thread().active_epoch_id,
            "harness": "fake",
            "native_session_id": "ses_fake",
            "native_message_id": "native_hook_user",
            "role": "user",
            "kind": "conversation",
            "content": "original",
            "reasoning": None,
            "refs": [],
            "created_at": NOW,
        },
    )
    manager.append(transaction.id, Operation(op="append", stream="messages", record=message))
    manager.commit(transaction.id)
    path = workspace.config.memory_root / "raw" / "conversations" / "messages.jsonl"
    original = path.read_text(encoding="utf-8")
    edited = original.replace('"content": "original"', '"content": "edited"')
    path.write_text(edited, encoding="utf-8")
    workspace.repository._git("add", str(path.relative_to(workspace.config.memory_root)))
    path.write_text(original, encoding="utf-8")
    status = workspace.repository.pre_commit_hook_status()
    result = subprocess.run(
        [status["path"]],
        cwd=workspace.config.memory_root,
        text=True,
        capture_output=True,
        check=False,
        env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert result.returncode == 1
    assert "APPEND_ONLY_VIOLATION" in result.stdout


def test_pre_commit_hook_rejects_unreceipted_protected_append(workspace) -> None:
    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    transaction = manager.begin(transaction_id="txn_raw_before_meta", fingerprint_context={"kind": "raw"})
    message = envelope(
        "msg_meta_evidence",
        "conversation-message/v1",
        {
            "thread_id": workspace.thread().thread_id,
            "epoch_id": workspace.thread().active_epoch_id,
            "harness": "fake",
            "native_session_id": "ses_fake",
            "native_message_id": "native_meta_evidence",
            "role": "user",
            "kind": "conversation",
            "content": "evidence",
            "reasoning": None,
            "refs": [],
            "created_at": NOW,
        },
    )
    manager.append(transaction.id, Operation(op="append", stream="messages", record=message))
    manager.commit(transaction.id)

    protected = meta()
    protected["payload"]["evidence_refs"] = ["message:msg_meta_evidence"]
    protected["payload"]["promotion_refs"] = []
    protected["payload"]["approval_ref"] = "forged_approval"
    path = workspace.config.memory_root / "meta" / "revisions.jsonl"
    original = path.read_text(encoding="utf-8")
    path.write_text(original + json.dumps(protected, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    workspace.repository._git("add", str(path.relative_to(workspace.config.memory_root)))
    path.write_text(original, encoding="utf-8")
    status = workspace.repository.pre_commit_hook_status()
    result = subprocess.run(
        [status["path"]],
        cwd=workspace.config.memory_root,
        text=True,
        capture_output=True,
        check=False,
        env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert result.returncode == 1
    assert "TRANSACTION_RECEIPT_REQUIRED" in result.stdout
