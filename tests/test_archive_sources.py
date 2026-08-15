from __future__ import annotations

import json
from pathlib import Path

from pco.checkpoint import CheckpointEngine
from pco.harness import FakeHarnessAdapter
from pco.archive import ConversationArchive
from pco.sources import SourceManager

from conftest import visible_messages


def test_turn_archive_is_incremental_and_omits_non_public_messages(workspace) -> None:
    archive = ConversationArchive(workspace)
    messages = visible_messages() + [
        {"native_message_id": "tool_1", "role": "tool", "content": "secret tool output"},
        {"native_message_id": "system_1", "role": "system", "content": "hidden prompt"},
    ]
    first = archive.archive(messages)
    second = archive.archive(messages)
    assert first["archived"] == 2
    assert second["archived"] == 0


def test_archive_dedups_after_crash_between_commit_and_cursor_update(workspace) -> None:
    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages())
    engine = CheckpointEngine(workspace, adapter)
    first = engine.archive.archive(adapter.messages)
    assert first["archived"] == 2
    # Simulate a crash after the canonical commit but before the cursor update.
    thread = workspace.thread()
    thread.last_archived_message_id = None
    thread.archive_cursor = None
    workspace.save_thread(thread)
    second = engine.archive.archive(adapter.messages)
    assert second["archived"] == 0
    assert workspace.thread().archive_cursor == "native_assistant_1"
    stored = list(workspace.repository.iter_records("messages"))
    assert [record["payload"]["role"] for record in stored] == ["user", "assistant"]
    assert stored[1]["payload"]["reasoning"] == "exposed reasoning"


def test_deduplicated_retry_recovers_archive_cursor(workspace) -> None:
    archive = ConversationArchive(workspace)
    messages = visible_messages()
    archive.archive(messages)
    thread = workspace.thread()
    thread.archive_cursor = None
    thread.last_archived_message_id = None
    workspace.save_thread(thread)

    retried = archive.archive(messages)
    recovered = workspace.thread()
    assert retried["archived"] == 0
    assert recovered.archive_cursor == "native_assistant_1"
    assert recovered.last_archived_message_id == "msg_assistant_1"


def test_archive_deduplicates_a_large_crash_recovery_batch(workspace) -> None:
    messages = [
        {
            "id": f"msg_{index}",
            "native_message_id": f"native_{index}",
            "role": "user" if index % 2 == 0 else "assistant",
            "kind": "conversation",
            "content": f"message {index}",
        }
        for index in range(600)
    ]
    archive = ConversationArchive(workspace)
    assert archive.archive(messages)["archived"] == 600

    # Simulate the crash window after canonical commit and before the cursor
    # update. The retry must deduplicate the entire committed batch.
    thread = workspace.thread()
    thread.last_archived_message_id = None
    thread.archive_cursor = None
    workspace.save_thread(thread)

    assert archive.archive(messages)["archived"] == 0
    stored = list(workspace.repository.iter_records("messages"))
    assert len(stored) == 600
    assert len({record["payload"]["native_message_id"] for record in stored}) == 600
    assert workspace.thread().archive_cursor == "native_599"


def test_archive_tail_reader_discards_a_chunk_boundary_fragment(workspace) -> None:
    archive = ConversationArchive(workspace)
    path = workspace.repository.profile.stream_path(workspace.config.memory_root, "messages")
    path.parent.mkdir(parents=True, exist_ok=True)

    # 100 records of exactly 1,025 bytes make the 64 KiB read begin 64 bytes
    # into record 36. The first chunk therefore contains a truncated JSONL
    # record whose terminating newline is part of the 64-line window.
    lines: list[bytes] = []
    for index in range(100):
        message = {
            "id": f"msg_{index:03d}",
            "native_message_id": f"native_{index:03d}",
            "role": "user",
            "kind": "conversation",
            "content": "",
        }
        while True:
            record = archive._record(message)
            line = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
            if len(line) >= 1025:
                break
            message["content"] += "x" * (1025 - len(line))
        assert len(line) == 1025
        lines.append(line)
    path.write_bytes(b"".join(lines))

    keys = archive._tail_native_keys(limit=64)
    assert {key[2] for key in keys} == {f"native_{index:03d}" for index in range(36, 100)}


def test_reasoning_is_not_fabricated_or_saved_when_disabled(workspace) -> None:
    workspace.config.archive_reasoning = False
    ConversationArchive(workspace).archive(visible_messages())
    stored = list(workspace.repository.iter_records("messages"))
    assert stored[0]["payload"]["reasoning"] is None
    assert stored[1]["payload"]["reasoning"] is None


def test_source_snapshot_and_diff_only_advance_with_transaction(workspace, tmp_path: Path) -> None:
    source_path = tmp_path / "journal.md"
    source_path.write_text("第一版日记\n", encoding="utf-8")
    sources = SourceManager(workspace)
    registration = sources.register_local(source_path)
    duplicate = sources.register_local(source_path)
    assert duplicate["idempotent"] and duplicate["source_id"] == registration["source_id"]

    first = sources.collect_diffs()
    assert len(first["changes"]) == 1
    assert "第一版日记" in first["changes"][0]["diff"]
    snapshot = workspace.config.memory_root / registration["record"]["payload"]["snapshot_path"]
    assert not snapshot.exists()

    from mem_core.transaction import TransactionManager

    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    transaction = manager.begin(transaction_id="txn_snapshot", fingerprint_context={"kind": "snapshot"})
    for operation in first["operations"]:
        manager.append(transaction.id, operation)
    manager.commit(transaction.id)
    assert snapshot.read_text(encoding="utf-8") == "第一版日记\n"
    assert sources.collect_diffs()["changes"] == []

    source_path.write_text("第二版日记\n新增一段。\n", encoding="utf-8")
    changed = sources.collect_diffs()
    assert len(changed["changes"]) == 1
    assert "-第一版日记" in changed["changes"][0]["diff"]
    assert "+第二版日记" in changed["changes"][0]["diff"]
