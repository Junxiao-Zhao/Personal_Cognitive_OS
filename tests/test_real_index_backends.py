from __future__ import annotations

import os

import pytest

from mem_core.models import Operation
from mem_core.transaction import TransactionManager
from pco.archive import ConversationArchive
from pco.retrieval import build_index, search

from conftest import event, visible_messages


@pytest.mark.milvus
@pytest.mark.skipif(os.getenv("PCO_RUN_MILVUS") != "1", reason="Milvus Lite needs a loopback port")
def test_real_milvus_lite_and_tantivy_generation(workspace) -> None:
    ConversationArchive(workspace).archive(visible_messages())
    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    transaction = manager.begin(transaction_id="txn_real_index", fingerprint_context={"kind": "index-fixture"})
    manager.append(transaction.id, Operation(op="append", stream="events", record=event()))
    manager.commit(transaction.id)

    built = build_index(
        repo_root=workspace.config.memory_root,
        indexes_root=workspace.config.indexes_root,
        force=True,
    )
    assert built["dense_backend"] == "milvus-lite"
    assert built["lexical_backend"] == "tantivy"
    assert "backend_errors" not in built

    result = search(
        repo_root=workspace.config.memory_root,
        indexes_root=workspace.config.indexes_root,
        query="公开成果拖延",
        mode="pattern",
    )
    assert result["backends"] == {"dense": "milvus-lite", "lexical": "tantivy"}
    assert any(item["id"] == "evt_publish_delay" for item in result["results"])
