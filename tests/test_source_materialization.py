from __future__ import annotations

from pathlib import Path

from pco.sources import ReaderRegistry, SourceManager


def test_local_reader_uses_materialization_contract_and_wrapper_normalization(workspace, tmp_path: Path) -> None:
    source_path = tmp_path / "notes.md"
    source_path.write_bytes("第一行\r\n\r\n".encode("utf-8"))
    manager = SourceManager(workspace)
    registration = manager.register_local(source_path)

    materialized = manager.materialize(registration["record"])

    assert set(materialized) == {"locator", "reader", "normalized_content", "media_type", "read_metadata"}
    assert materialized["reader"] == "local-readonly"
    assert materialized["normalized_content"] == "第一行\n"
    assert materialized["media_type"] == "text/markdown"
    assert materialized["read_metadata"]["path"] == str(source_path.resolve())


def test_fake_remote_reader_materializes_without_network_and_wrapper_owns_snapshot(workspace) -> None:
    calls: list[str] = []
    content = {"https://fake.invalid/doc/42": "remote v1\r\n"}

    def fake_reader(locator: str) -> dict[str, object]:
        calls.append(locator)
        return {
            "locator": locator,
            "reader": "fake-remote",
            "normalized_content": content[locator],
            "media_type": "text/markdown",
            "read_metadata": {"credential": "must-not-be-persisted", "request_id": "fixture-1"},
        }

    readers = ReaderRegistry()
    readers.register("fake-remote", fake_reader)
    manager = SourceManager(workspace, readers)
    registration = manager.register_locator(
        "https://fake.invalid/doc/42",
        reader_skill="fake-remote",
        provider="fake_remote",
        display_name="Fixture document",
    )

    first = manager.collect_diffs()
    assert len(first["changes"]) == 1
    assert first["changes"][0]["source_revision"] == 2
    assert [operation.op for operation in first["operations"]] == ["append", "write_artifact"]
    assert calls == ["https://fake.invalid/doc/42"]
    assert "credential" not in str(first)

    # The wrapper transaction is the only operation that creates the snapshot.
    from mem_core.transaction import TransactionManager

    transaction_manager = TransactionManager(workspace.repository, workspace.config.state_root)
    transaction = transaction_manager.begin(transaction_id="txn_fake_snapshot", fingerprint_context={"kind": "source_snapshot"})
    for operation in first["operations"]:
        transaction_manager.append(transaction.id, operation)
    transaction_manager.commit(transaction.id)
    snapshot = workspace.config.memory_root / registration["record"]["payload"]["snapshot_path"]
    assert snapshot.read_text(encoding="utf-8") == "remote v1\n"

    content["https://fake.invalid/doc/42"] = "remote v2\nnew line\n"
    changed = manager.collect_diffs()
    assert "-remote v1" in changed["changes"][0]["diff"]
    assert "+remote v2" in changed["changes"][0]["diff"]


def test_worker_contract_explicitly_excludes_source_snapshot_write_permission(workspace) -> None:
    from pco.checkpoint import CheckpointEngine
    from pco.harness import FakeHarnessAdapter

    contract = CheckpointEngine(workspace, FakeHarnessAdapter(workspace.config.state_root))._worker_profile_contract()

    assert contract["source_materialization"]["snapshot_write_permission"] is False
    assert "sources" not in contract["allowed_streams"]
    assert any("source snapshot" in invariant for invariant in contract["required_invariants"])
