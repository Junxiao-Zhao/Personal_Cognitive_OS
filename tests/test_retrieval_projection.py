from __future__ import annotations

import json
from pathlib import Path
import subprocess

from mem_core.models import Operation
from mem_core.transaction import TransactionManager
from pco.checkpoint import CheckpointEngine
from pco.context import render
from pco.harness import FakeHarnessAdapter, WorkerResult
from pco.projections import project_affine, project_markdown
from pco.retrieval import _chunks, build_index, search, tokenize

from conftest import NOW, continuation, envelope, event, hypothesis, meta, visible_messages


def _seed(workspace, *, with_meta: bool = False):
    operations = [
        Operation(op="append", stream="events", record=event()),
        Operation(op="append", stream="hypotheses", record=hypothesis()),
        Operation(op="append", stream="continuations", record=continuation()),
    ]
    if with_meta:
        operations.insert(2, Operation(op="append", stream="meta_revisions", record=meta()))
    adapter = FakeHarnessAdapter(
        workspace.config.state_root,
        messages=visible_messages(),
        worker=lambda _payload: WorkerResult(operations=operations),
    )
    engine = CheckpointEngine(workspace, adapter)
    result = engine.request("manual")
    if with_meta:
        assert result["approval_required"]
        result = engine.decide("yes")
    return adapter, result


def test_five_retrieval_modes_return_evidence_time_and_qualification(workspace) -> None:
    _seed(workspace)
    for mode in ("continuity", "current", "pattern", "historical", "change"):
        result = search(
            repo_root=workspace.config.memory_root,
            query="公开成果 拖延 评价",
            mode=mode,
            limit=5,
        )
        assert result["results"], mode
        for item in result["results"]:
            assert item["retrieval_mode"] == mode
            assert {"id", "revision", "text", "recorded_at", "occurred_at", "dense_score", "lexical_score", "rrf_score", "time_score", "evidence_refs", "links", "current", "assistant_context", "user_evidence_eligible"} <= item.keys()
    change = search(repo_root=workspace.config.memory_root, query="评价", mode="change", limit=10)
    assert change["change_windows"]["caution"].startswith("Missing records")


def test_turn_chunker_splits_oversized_messages_without_indexing_reasoning(workspace) -> None:
    adapter = FakeHarnessAdapter(
        workspace.config.state_root,
        messages=[
            {
                "id": "msg_long_user",
                "native_message_id": "native_long_user",
                "role": "user",
                "kind": "conversation",
                "content": "评价边界" * 300,
                "created_at": NOW,
            },
            {
                "id": "msg_long_assistant",
                "native_message_id": "native_long_assistant",
                "role": "assistant",
                "kind": "conversation",
                "content": "收到。",
                "reasoning": "never index this reasoning",
                "created_at": NOW,
            },
        ],
    )
    from pco.archive import ConversationArchive

    ConversationArchive(workspace).archive(adapter.messages)
    stored = list(workspace.repository.iter_records("messages"))
    chunks = _chunks(stored, turns_per_chunk=2, overlap=1, token_budget=100)
    assert len(chunks) > 2
    assert len({chunk["id"] for chunk in chunks}) == len(chunks)
    assert all(len(tokenize(chunk["text"])) <= 110 for chunk in chunks)
    assert all("never index this reasoning" not in chunk["text"] for chunk in chunks)


def test_reasoning_is_archived_but_not_indexed_or_context_injected(workspace) -> None:
    _seed(workspace)
    index = build_index(repo_root=workspace.config.memory_root, indexes_root=workspace.config.indexes_root)
    documents = json.loads((Path(index["generation_path"]) / "documents.json").read_text(encoding="utf-8"))
    assert all("exposed reasoning" not in document["text"] for document in documents)

    context_path = workspace.config.state_root / "context" / "test-current.md"
    render(repo_root=workspace.config.memory_root, output_path=context_path)
    content = context_path.read_text(encoding="utf-8")
    assert "exposed reasoning" not in content
    assert "我每次准备公开" not in content
    assert "公开成果前的拖延" in content


def test_current_mode_excludes_old_meta_but_historical_includes_it(workspace) -> None:
    _seed(workspace, with_meta=True)
    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    transaction = manager.begin(transaction_id="txn_meta_v2", fingerprint_context={"checkpoint_id": "ckpt_meta_v2"})
    updated = meta()
    updated["revision"] = 2
    updated["payload"]["previous_revision"] = "meta_current@1"
    updated["payload"]["change_summary"] = "用户澄清核心更接近厌恶评价。"
    updated["payload"]["sections"]["active_patterns"] = ["公开表达前会关注评价边界。"]
    manager.append(transaction.id, Operation(op="append", stream="meta_revisions", record=updated))
    manager.attach_approval(
        transaction.id,
        checkpoint_id="ckpt_meta_v2",
        proposal_hash_value=manager.load(transaction.id).proposal_hash,
        receipt_id="approval_pending",
    )
    manager.commit(transaction.id)

    current = search(repo_root=workspace.config.memory_root, query="认识", mode="current", limit=100)
    historical = search(repo_root=workspace.config.memory_root, query="认识", mode="historical", limit=100)
    current_meta = [item for item in current["results"] if item["stream"] == "meta_revisions"]
    historical_meta = [item for item in historical["results"] if item["stream"] == "meta_revisions"]
    assert [item["revision"] for item in current_meta] == [2]
    assert {item["revision"] for item in historical_meta} == {1, 2}
    assert all(item["policy_version"] == "promotion@0.3" for item in historical_meta)
    assert next(item for item in historical_meta if item["revision"] == 1)["revision_reason"] == "建立第一版有边界的当前认识。"


def test_historical_mode_exposes_revision_policy_and_reason(workspace) -> None:
    _seed(workspace)
    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    transaction = manager.begin(transaction_id="txn_hypothesis_v2", fingerprint_context={"kind": "correction"})
    revised = hypothesis(status="disputed")
    revised["revision"] = 2
    revised["payload"]["revision_reason"] = "用户明确否定了害怕失败这一解释。"
    revised["payload"]["counter_evidence_refs"] = ["message:msg_user_1"]
    manager.append(transaction.id, Operation(op="append", stream="hypotheses", record=revised))
    manager.commit(transaction.id)

    historical = search(repo_root=workspace.config.memory_root, query="害怕失败", mode="historical", limit=100)
    revisions = [
        item for item in historical["results"]
        if item["stream"] == "hypotheses" and item["id"] == "hyp_evaluation"
    ]
    assert {item["revision"] for item in revisions} == {1, 2}
    assert all(item["policy_version"] == "promotion@0.3" for item in revisions)
    assert next(item for item in revisions if item["revision"] == 2)["revision_reason"].startswith("用户明确否定")


def test_backlinks_and_replaceable_projections_are_idempotent(workspace) -> None:
    _seed(workspace)
    canonical_before = workspace.repository.head()
    markdown = project_markdown(repo_root=workspace.config.memory_root, output_root=workspace.config.projection_root / "markdown")
    assert markdown["ok"] and markdown["pages"] >= 4
    home = workspace.config.projection_root / "markdown" / "home" / "pco_home.md"
    assert "pco://" not in home.read_text(encoding="utf-8")
    assert (workspace.config.projection_root / "markdown" / "indexes" / "index_events.md").is_file()
    assert project_markdown(repo_root=workspace.config.memory_root, output_root=workspace.config.projection_root / "markdown")["idempotent"]

    bridge = Path(__file__).parent / "fixtures" / "affine_bridge.py"
    affine = project_affine(
        repo_root=workspace.config.memory_root,
        state_root=workspace.config.state_root,
        command=f"python {bridge}",
    )
    assert affine["ok"] and affine["pages"] == markdown["pages"]
    assert project_affine(repo_root=workspace.config.memory_root, state_root=workspace.config.state_root, command=f"python {bridge}")["idempotent"]
    assert workspace.repository.head() == canonical_before


def test_clone_rebuilds_all_replaceable_derivations(workspace, tmp_path: Path) -> None:
    _seed(workspace)
    canonical_commit = workspace.repository.head()
    clone = tmp_path / "memory-clone"
    subprocess.run(
        ["git", "clone", "--quiet", str(workspace.config.memory_root), str(clone)],
        check=True,
        capture_output=True,
        text=True,
    )

    indexes = tmp_path / "rebuilt-indexes"
    rebuilt = build_index(repo_root=clone, indexes_root=indexes)
    assert rebuilt["memory_commit"] == canonical_commit
    assert rebuilt["dense_backend"] in {"milvus-lite", "local-hashed-vector"}
    assert rebuilt["lexical_backend"] in {"tantivy", "local-inverted-index"}
    assert (Path(rebuilt["generation_path"]) / "backlinks.json").is_file()

    markdown = project_markdown(repo_root=clone, output_root=tmp_path / "rebuilt-markdown")
    bridge = Path(__file__).parent / "fixtures" / "affine_bridge.py"
    affine = project_affine(
        repo_root=clone,
        state_root=tmp_path / "rebuilt-state",
        command=f"python {bridge}",
    )
    assert markdown["ok"] and markdown["memory_commit"] == canonical_commit
    assert affine["ok"] and affine["memory_commit"] == canonical_commit


def test_affine_failure_is_reported_after_commit_and_retry_is_idempotent(workspace, monkeypatch) -> None:
    workspace.config.checkpoint.derivations.projection = "affine"
    monkeypatch.delenv("PCO_AFFINE_COMMAND", raising=False)
    adapter, result = _seed(workspace)
    committed = workspace.repository.head()
    assert result["status"] == "COMMITTED_WITH_PENDING_DERIVATIONS"
    assert result["receipt"]["derivations"]["projection"]["error"]["code"] == "AFFINE_BRIDGE_NOT_CONFIGURED"
    assert adapter.receipts[-1]["status"] == "COMMITTED_WITH_PENDING_DERIVATIONS"

    bridge = Path(__file__).parent / "fixtures" / "affine_bridge.py"
    monkeypatch.setenv("PCO_AFFINE_COMMAND", f"python {bridge}")
    retried = CheckpointEngine(workspace, adapter).retry_derivations()
    assert retried["status"] == "DONE"
    assert workspace.repository.head() == committed
    assert adapter.receipts[-1]["status"] == "DONE"


def test_concept_requires_external_search_receipt(workspace) -> None:
    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    invalid = envelope(
        "psy_evaluation",
        "pco/psychology/v1",
        {
            "name": "评价焦虑",
            "description": "对他人评价的担忧。",
            "aliases": [],
            "external_refs": [
                {
                    "url": "https://example.org/concept",
                    "title": "Concept reference",
                    "accessed_at": NOW,
                    "search_receipt": "",
                }
            ],
            "status": "active",
        },
    )
    transaction = manager.begin(transaction_id="txn_bad_concept", fingerprint_context={"kind": "concept"})
    manager.append(transaction.id, Operation(op="append", stream="psychologies", record=invalid))
    from mem_core.errors import MemError
    import pytest

    with pytest.raises(MemError) as error:
        manager.validate(transaction.id)
    assert error.value.detail.code == "SCHEMA_VALIDATION_FAILED"
    assert error.value.detail.path.endswith("/search_receipt")
