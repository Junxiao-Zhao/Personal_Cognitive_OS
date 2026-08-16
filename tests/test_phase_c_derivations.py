from __future__ import annotations

from pathlib import Path
import json

from mem_core.errors import MemError
from pco.checkpoint.derivations import run_derivations
from pco.checkpoint.errors import derivation_error, failed_attempt, result_attempt
from pco.checkpoint.state import CheckpointState


def test_derivation_errors_keep_memerror_shape_and_wrap_unknown() -> None:
    known = derivation_error(
        MemError("CAPABILITY_NOT_FOUND", "profile_invoke", "Missing capability"),
        "index",
    )
    assert known == {
        "code": "CAPABILITY_NOT_FOUND",
        "phase": "profile_invoke",
        "message": "Missing capability",
        "retryable": False,
        "recovery": [],
    }

    unknown = derivation_error(RuntimeError("backend exploded"), "projection")
    assert unknown["code"] == "UNEXPECTED_DERIVATION_FAILURE"
    assert unknown["phase"] == "projection"
    assert unknown["retryable"] is True
    assert unknown["recovery"]


def test_derivation_retry_preserves_first_error_and_appends_attempt() -> None:
    first = failed_attempt({}, RuntimeError("first"), "context")
    second = result_attempt(first, {"ok": True, "content_hash": "sha256:test"}, "context")

    assert second["ok"] is True
    assert second["error"]["message"] == "first"
    assert len(second["attempts"]) == 2
    assert second["attempts"][1]["recovered_from"]["code"] == "UNEXPECTED_DERIVATION_FAILURE"


def test_derivation_attempt_normalizes_unserializable_memerror_value() -> None:
    failed = failed_attempt(
        {},
        MemError("BACKEND_INVALID", "index", "bad backend value", value=object(), retryable=True),
        "index",
    )
    json.dumps(failed)
    assert failed["error"]["code"] == "BACKEND_INVALID"
    assert isinstance(failed["error"]["value"], str)


def test_checkpoint_derivation_record_preserves_results_and_structured_error_details() -> None:
    from pco.checkpoint.finalize import _checkpoint_derivations

    state = CheckpointState(
        id="ckpt_derivation_shape",
        trigger="manual",
        status="COMMITTED_WITH_PENDING_DERIVATIONS",
        after_message_id=None,
        through_message_id="msg_through",
        thread_id="thread",
        harness_binding_id="binding",
        parent_session_id="session",
        derivations={
            "projection": {
                "ok": True,
                "content_hash": "sha256:projection",
                "error": {
                    "code": "PROJECTION_RECOVERED",
                    "phase": "projection",
                    "path": "/target",
                    "record_id": "projection-1",
                    "recovery": ["retry"],
                    "value": object(),
                },
                "attempts": [{"attempt": 1}],
            }
        },
    )

    result = _checkpoint_derivations(state)
    projection = result["projection"]
    assert projection["ok"] is True
    assert projection["content_hash"] == "sha256:projection"
    assert projection["error"]["code"] == "PROJECTION_RECOVERED"
    assert projection["error"]["path"] == "/target"
    assert projection["error"]["record_id"] == "projection-1"
    assert projection["error"]["recovery"] == ["retry"]
    assert projection["attempts"] == [{"attempt": 1}]


def test_checkpoint_derivations_dispatch_through_profile_and_pin_content_commit(workspace, monkeypatch) -> None:
    workspace.config.checkpoint.derivations.index = True
    workspace.config.checkpoint.derivations.backlinks = True
    workspace.config.checkpoint.derivations.projection = "markdown"
    calls: list[tuple[str, dict]] = []

    def invoke(name: str, **kwargs):
        calls.append((name, kwargs))
        return {"ok": True, "capability": name}

    monkeypatch.setattr(type(workspace.profile), "invoke", lambda _self, name, **kwargs: invoke(name, **kwargs))
    from types import SimpleNamespace

    engine = SimpleNamespace(workspace=workspace)
    state = CheckpointState(
        id="ckpt_phase_c",
        trigger="manual",
        status="MEMORY_COMMITTED",
        after_message_id=None,
        through_message_id="msg_through",
        thread_id=workspace.thread().thread_id,
        harness_binding_id=workspace.binding().id,
        parent_session_id="ses_main",
        content_commit=workspace.repository.head(),
    )
    run_derivations(engine, state)

    assert {name for name, _ in calls} == {"index.build", "backlinks.build", "projections.markdown"}
    assert all(kwargs["source_commit"] == state.content_commit for _, kwargs in calls)
    assert all(item["ok"] for item in state.derivations.values())


def test_context_bundle_reports_the_requested_source_commit(workspace, tmp_path) -> None:
    from pco.context import render

    commit = workspace.repository.head()
    bundle = render(
        repo_root=workspace.config.memory_root,
        output_path=tmp_path / "current.md",
        source_commit=commit,
    )
    assert bundle["source_commit"] == commit
    assert bundle["content_hash"].startswith("sha256:")


def test_audit_head_resolves_to_checkpoint_content_commit(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace
    import pco.repo_loader as repo_loader

    class FakeRepository:
        def head(self):
            return "audit_commit"

        def current_records(self, stream):
            assert stream == "checkpoints"
            return {
                "ckpt_one": {
                    "payload": {
                        "audit_transaction_id": "txn_audit",
                        "content_commit": "content_commit",
                    }
                }
            }

    class FakeManager:
        def __init__(self, repository, state_root):
            assert state_root == tmp_path / "state"

        def load(self, transaction_id):
            assert transaction_id == "txn_audit"
            return SimpleNamespace(commit="audit_commit")

    monkeypatch.setattr(repo_loader, "repository_for_repo", lambda _root: FakeRepository())
    monkeypatch.setattr(repo_loader, "TransactionManager", FakeManager)
    assert repo_loader.resolve_derivation_source_commit(tmp_path / "memory", state_root=tmp_path / "state") == "content_commit"


def test_opencode_system_transform_uses_memory_cache() -> None:
    plugin = (Path(__file__).parents[1] / "packages/pco/src/pco/resources/opencode/plugins/pco.ts").read_text(encoding="utf-8")
    transform = plugin.split('"experimental.chat.system.transform"', 1)[1]
    assert "current.json" in plugin
    assert "refreshContextCache(result)" in plugin
    assert "readFileSync(contextPath" not in transform
