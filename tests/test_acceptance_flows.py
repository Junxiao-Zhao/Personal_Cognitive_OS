from __future__ import annotations

import os
from pathlib import Path

import pytest

from mem_core.models import Operation
from pco.checkpoint import CheckpointEngine
from pco.harness import FakeHarnessAdapter, WorkerResult
from pco.retrieval import search
from pco.sources import SourceManager

from conftest import NOW, approval_grant, continuation, envelope, hypothesis, meta, visible_messages


needs_loopback = pytest.mark.skipif(
    os.getenv("PCO_RUN_MILVUS") != "1",
    reason="Milvus Lite needs a loopback port",
)


def _concept(record_id: str, schema_version: str, name: str) -> dict:
    return envelope(
        record_id,
        schema_version,
        {
            "name": name,
            "description": f"用于探索的{name}概念，不代表对用户的诊断。",
            "aliases": [],
            "external_refs": [
                {
                    "url": "https://example.org/reference",
                    "title": "Reference definition",
                    "accessed_at": NOW,
                    "search_receipt": "worker_value_is_replaced",
                }
            ],
            "status": "active",
        },
    )


def _search_receipt() -> dict:
    return envelope(
        "search_fixture_reference",
        "pco/search-receipt/v1",
        {
            "worker_session_id": "ses_fake_worker",
            "call_id": "call_fixture",
            "tool": "websearch",
            "input": {"query": "reference definition"},
            "output_excerpt": "Result: https://example.org/reference",
            "status": "completed",
        },
    )


def test_ac01_source_cold_start_commits_four_classes_meta_and_continuation(workspace, tmp_path: Path) -> None:
    journal = tmp_path / "journal.md"
    journal.write_text("我常在公开作品前拖延，也很在意是否能真实表达。\n", encoding="utf-8")
    registration = SourceManager(workspace).register_local(journal)
    assert not workspace.repository.current_records("meta_revisions")
    assert not (workspace.config.memory_root / registration["record"]["payload"]["snapshot_path"]).exists()

    psychology = _concept("psy_evaluation", "pco/psychology/v1", "评价顾虑")
    philosophy = _concept("phi_authenticity", "pco/philosophy/v1", "真实性")
    archetype = envelope(
        "arc_creator",
        "pco/archetype/v1",
        {
            "name": "谨慎的创作者",
            "description": "希望被看见，同时保护尚未稳定表达的创作者意象。",
            "aliases": [],
            "stance": "identify",
            "evidence_refs": ["message:msg_user_1"],
            "status": "active",
        },
    )
    linked_event = envelope(
        "evt_publish_delay",
        "pco/event/v1",
        {
            "occurred_at": {"start": "2026-08-03", "end": "2026-08-03", "precision": "day"},
            "description": "准备公开成果时延迟发布，并表达对评价的厌恶。",
            "links": {
                "psychologies": ["psy_evaluation"],
                "philosophies": ["phi_authenticity"],
                "archetypes": ["arc_creator"],
            },
            "evidence_refs": ["message:msg_user_1"],
            "revision_reason": "initial extraction",
            "status": "active",
        },
    )
    operations = [
        Operation(op="append", stream="psychologies", record=psychology),
        Operation(op="append", stream="philosophies", record=philosophy),
        Operation(op="append", stream="archetypes", record=archetype),
        Operation(op="append", stream="events", record=linked_event),
        Operation(op="append", stream="hypotheses", record=hypothesis()),
        Operation(op="append", stream="meta_revisions", record=meta()),
        Operation(op="append", stream="continuations", record=continuation()),
    ]
    adapter = FakeHarnessAdapter(
        workspace.config.state_root,
        messages=visible_messages(),
        worker=lambda _payload: WorkerResult(
            operations,
            search_receipts=[_search_receipt()],
            skill_versions={"pco-memory": "0.1.0"},
        ),
    )
    engine = CheckpointEngine(workspace, adapter)
    proposal = engine.request("manual")
    assert proposal["status"] == "AWAITING_META_APPROVAL"
    assert not workspace.repository.current_records("meta_revisions")

    result = engine.decide(
        "yes",
        question_request_id="question_test",
        approval_grant=approval_grant(proposal["proposal"]),
        session_id=adapter.session_id,
    )
    assert result["status"] == "DONE"
    for stream in ("events", "psychologies", "philosophies", "archetypes", "hypotheses", "meta_revisions", "continuations"):
        assert workspace.repository.current_records(stream), stream
    source = workspace.repository.current_records("sources")[registration["source_id"]]
    assert source["revision"] == 2 and source["payload"]["content_hash"].startswith("sha256:")
    snapshot = workspace.config.memory_root / source["payload"]["snapshot_path"]
    assert snapshot.read_text(encoding="utf-8").startswith("我常在公开作品前拖延")
    assert result["receipt"]["source_hashes"][registration["source_id"]] == source["payload"]["content_hash"]


@needs_loopback
def test_ac11_natural_language_correction_keeps_history_and_updates_current_meta(workspace) -> None:
    first_operations = [
        Operation(op="append", stream="hypotheses", record=hypothesis()),
        Operation(op="append", stream="meta_revisions", record=meta()),
        Operation(op="append", stream="continuations", record=continuation()),
    ]
    first_adapter = FakeHarnessAdapter(
        workspace.config.state_root,
        messages=visible_messages(),
        worker=lambda _payload: WorkerResult(first_operations),
    )
    first_engine = CheckpointEngine(workspace, first_adapter)
    first_pending = first_engine.request("manual")
    first_engine.decide(
        "yes",
        question_request_id="question_test",
        approval_grant=approval_grant(first_pending["proposal"]),
        session_id=first_adapter.session_id,
    )

    correction_messages = [
        {
            "id": "msg_user_2",
            "native_message_id": "native_user_2",
            "role": "user",
            "kind": "conversation",
            "content": "我不同意害怕失败的解释；更准确的是厌恶被评价。",
            "created_at": NOW,
        },
        {
            "id": "msg_assistant_2",
            "native_message_id": "native_assistant_2",
            "role": "assistant",
            "kind": "conversation",
            "content": "我会把这项纠正保留为新 revision。",
            "created_at": NOW,
        },
    ]
    revised_hypothesis = hypothesis(status="disputed")
    revised_hypothesis["revision"] = 2
    revised_hypothesis["payload"]["counter_evidence_refs"] = ["message:msg_user_2"]
    revised_hypothesis["payload"]["revision_reason"] = "用户明确否定害怕失败的解释。"
    revised_meta = meta()
    revised_meta["revision"] = 2
    revised_meta["payload"]["previous_revision"] = "meta_current@1"
    revised_meta["payload"]["evidence_refs"] = ["message:msg_user_2"]
    revised_meta["payload"]["promotion_refs"] = []
    revised_meta["payload"]["sections"]["active_patterns"] = ["公开表达前会主动确认评价边界。"]
    revised_meta["payload"]["change_summary"] = "按用户纠正，撤回害怕失败的解释。"
    revised_continuation = continuation(revision=2, through="msg_assistant_2")
    second_adapter = FakeHarnessAdapter(
        workspace.config.state_root,
        messages=visible_messages() + correction_messages,
        worker=lambda _payload: WorkerResult(
            [
                Operation(op="append", stream="hypotheses", record=revised_hypothesis),
                Operation(op="append", stream="meta_revisions", record=revised_meta),
                Operation(op="append", stream="continuations", record=revised_continuation),
            ]
        ),
    )
    second_engine = CheckpointEngine(workspace, second_adapter)
    pending = second_engine.request("manual")
    assert pending["approval_required"]
    second_engine.decide(
        "yes",
        question_request_id="question_test",
        approval_grant=approval_grant(pending["proposal"]),
        session_id=second_adapter.session_id,
    )

    history = workspace.repository.record_history("hypotheses", "hyp_evaluation")
    assert [record["revision"] for record in history] == [1, 2]
    assert history[0]["payload"]["status"] == "hypothesis"
    assert history[1]["payload"]["status"] == "disputed"
    assert workspace.repository.current_records("meta_revisions")["meta_current"]["revision"] == 2
    current = search(repo_root=workspace.config.memory_root, query="害怕失败", mode="current", limit=100)
    assert not any(item["stream"] == "hypotheses" and item["id"] == "hyp_evaluation" for item in current["results"])
