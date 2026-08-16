from __future__ import annotations

from pco.backlinks import build
from pco.projections import _pages

from conftest import NOW, hypothesis


def test_backlinks_separate_current_and_historical_revisions(workspace) -> None:
    first = hypothesis()
    first["payload"]["evidence_refs"] = ["message:msg_user_2"]
    first["payload"]["counter_evidence_refs"] = ["message:msg_user_1"]
    workspace.repository.append_record(workspace.config.memory_root, "hypotheses", first)

    second = hypothesis()
    second["revision"] = 2
    second["recorded_at"] = "2026-08-04T10:00:00+00:00"
    second["payload"]["evidence_refs"] = ["message:msg_user_2"]
    second["payload"]["counter_evidence_refs"] = ["message:msg_user_1"]
    workspace.repository.append_record(workspace.config.memory_root, "hypotheses", second)

    third = hypothesis()
    third["revision"] = 3
    third["recorded_at"] = "2026-08-05T10:00:00+00:00"
    third["payload"]["evidence_refs"] = ["message:msg_user_2"]
    third["payload"]["counter_evidence_refs"] = []
    workspace.repository.append_record(workspace.config.memory_root, "hypotheses", third)

    result = build(repo_root=workspace.config.memory_root)

    assert result["backlinks"] == result["current_backlinks"]
    assert "message:msg_user_1" not in result["current_backlinks"]
    history = result["historical_backlinks"]["message:msg_user_1"]
    assert [item["source_revision"] for item in history] == [1, 2]
    assert all(item["status"] == "hypothesis" for item in history)
    assert [item["recorded_at"] for item in history] == [NOW, "2026-08-04T10:00:00+00:00"]


def test_current_backlinks_deduplicate_repeated_edges_and_projections_use_them(workspace) -> None:
    first = hypothesis()
    first["payload"]["evidence_refs"] = ["message:msg_user_1"]
    workspace.repository.append_record(workspace.config.memory_root, "hypotheses", first)

    second = hypothesis()
    second["revision"] = 2
    second["payload"]["evidence_refs"] = ["message:msg_user_1"]
    workspace.repository.append_record(workspace.config.memory_root, "hypotheses", second)

    third = hypothesis()
    third["revision"] = 3
    third["payload"]["evidence_refs"] = ["message:msg_user_1"]
    workspace.repository.append_record(workspace.config.memory_root, "hypotheses", third)

    result = build(repo_root=workspace.config.memory_root)

    assert result["current_backlinks"]["message:msg_user_1"] == [
        {"source_stream": "hypotheses", "source_id": "hyp_evaluation", "relation": "evidence_refs"}
    ]
    assert len(result["historical_backlinks"]["message:msg_user_1"]) == 3


def test_projections_consume_current_backlinks(monkeypatch, workspace) -> None:
    record = hypothesis()
    workspace.repository.append_record(workspace.config.memory_root, "hypotheses", record)
    monkeypatch.setattr(
        "pco.projections.build_backlinks",
        lambda **_: {
            "current_backlinks": {
                "hyp_evaluation": [
                    {"source_stream": "events", "source_id": "evt_current", "relation": "evidence"}
                ]
            },
            "historical_backlinks": {
                "hyp_evaluation": [
                    {"source_stream": "events", "source_id": "evt_historical", "relation": "evidence"}
                ]
            },
        },
    )

    pages = _pages(workspace.repository)
    page = next(item for item in pages if item["entity_id"] == "hyp_evaluation")

    assert "events:evt_current" in page["content"]
    assert "events:evt_historical" not in page["content"]
