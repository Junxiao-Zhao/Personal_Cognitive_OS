from __future__ import annotations

import pytest

from mem_core.errors import MemError
from mem_core.models import Operation, proposal_hash
from pco.checkpoint import CheckpointEngine, CheckpointState
from pco.checkpoint import finalize as finalize_steps
from pco.harness import FakeHarnessAdapter, WorkerResult
from pco.sources import SourceManager
from pco.workspace import Workspace

from conftest import NOW, approval_grant, continuation, envelope, event, hypothesis, meta, visible_messages


def basic_worker(_payload) -> WorkerResult:
    return WorkerResult(
        operations=[
            Operation(op="append", stream="events", record=event()),
            Operation(op="append", stream="hypotheses", record=hypothesis()),
            Operation(op="append", stream="continuations", record=continuation()),
        ],
        skill_versions={"pco-consolidate": "0.1.0"},
    )


def followup_worker(_payload) -> WorkerResult:
    result = basic_worker(_payload)
    for operation in result.operations:
        operation.record["revision"] = 2
    return result


@pytest.mark.parametrize("trigger", ["manual", "auto"])
def test_manual_and_auto_share_checkpoint_path(workspace, trigger: str) -> None:
    adapter = FakeHarnessAdapter(
        workspace.config.state_root,
        messages=visible_messages(),
        worker=basic_worker,
        context_usage=0.75,
    )
    result = CheckpointEngine(workspace, adapter).request(trigger)
    assert result["ok"]
    assert result["receipt"]["trigger"] == trigger
    assert adapter.compact_calls == 1
    assert len(adapter.published) == 1
    assert any(action.startswith("lock:") for action in adapter.actions)
    assert adapter.actions.index("publish_context") < adapter.actions.index("compact")
    assert adapter.actions.index("compact") < adapter.actions.index("insert_receipt")
    assert adapter.actions.index("insert_receipt") < adapter.actions.index("unlock")
    assert adapter.actions.index("close_worker") < adapter.actions.index("insert_receipt")
    assert adapter.actions.index("insert_receipt") < adapter.actions.index("unlock")
    assert workspace.thread().last_consolidated_message_id == "msg_assistant_1"
    assert workspace.repository.current_records("events")["evt_publish_delay"]
    frozen = workspace.load_json(f"checkpoints/{result['checkpoint_id']}/frozen.json")
    contract = frozen["profile_contract"]
    assert contract["allowed_streams"]["continuations"]["write_policy"] == "auto"
    assert contract["allowed_streams"]["continuations"]["record_schema"]["required"]
    assert "messages" not in contract["allowed_streams"]


def test_repeated_noop_compact_preserves_registered_source_hash_baseline(workspace, tmp_path) -> None:
    source_path = tmp_path / "registered.md"
    source_path.write_text("稳定 source\n", encoding="utf-8")
    registration = SourceManager(workspace).register_local(source_path)
    source_id = registration["source_id"]
    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages(), worker=basic_worker)
    engine = CheckpointEngine(workspace, adapter)

    first = engine.request("manual", intent="compact")
    baseline = first["receipt"]["source_hashes"]
    assert source_id in baseline
    assert adapter.worker_calls == 1
    head = workspace.repository.head()
    checkpoint_count = len(list(workspace.repository.iter_records("checkpoints")))
    transaction_path = workspace.config.memory_root / "transactions" / "transactions.jsonl"
    transaction_count = len(transaction_path.read_text(encoding="utf-8").splitlines())

    second = engine.request("manual", intent="compact")
    assert second["receipt"]["consolidation"]["status"] == "no_op"
    assert second["receipt"]["source_hashes"] == baseline
    assert second["receipt"]["canonical_transaction"]["created"] is False
    assert second["receipt"]["audit_transaction_id"] is None
    assert CheckpointEngine(workspace, adapter).status()["canonical_transaction"]["created"] is False
    assert workspace.repository.head() == head
    assert len(list(workspace.repository.iter_records("checkpoints"))) == checkpoint_count
    assert len(transaction_path.read_text(encoding="utf-8").splitlines()) == transaction_count
    assert adapter.worker_calls == 1

    third = engine.request("manual", intent="compact")
    assert third["receipt"]["consolidation"]["status"] == "no_op"
    assert third["receipt"]["source_hashes"] == baseline
    assert adapter.worker_calls == 1
    assert workspace.repository.head() == head
    assert len(list(workspace.repository.iter_records("checkpoints"))) == checkpoint_count
    assert len(transaction_path.read_text(encoding="utf-8").splitlines()) == transaction_count
    assert third["checkpoint_id"] not in workspace.repository.current_records("checkpoints")


def test_pending_compaction_keeps_input_locked_until_native_compact(workspace) -> None:
    pending = {
        "request_id": "request_overflow_1",
        "event_id": "event_overflow_1",
        "session_id": "ses_fake_main",
        "requested_boundary": {"through": "msg_assistant_1"},
        "requested_at": 1785686402000,
        "origin": "harness_auto_compaction",
    }

    class PendingDuringWorkerAdapter(FakeHarnessAdapter):
        def __init__(self, *args, workspace, **kwargs):
            self.workspace = workspace
            super().__init__(*args, **kwargs)
            self.merged = False

        def resume_worker(self, handle, payload):
            if not self.merged:
                self.merged = True
                CheckpointEngine(self.workspace, self).request(
                    "auto",
                    pending_compaction=pending,
                )
            return super().resume_worker(handle, payload)

    adapter = PendingDuringWorkerAdapter(
        workspace.config.state_root,
        workspace=workspace,
        messages=visible_messages(),
        worker=basic_worker,
    )
    result = CheckpointEngine(workspace, adapter).request("manual", intent="consolidate")
    assert result["ok"]
    assert adapter.actions.count("unlock") == 1
    assert "compact" in adapter.actions
    compact_index = adapter.actions.index("compact")
    assert "unlock" not in adapter.actions[:compact_index]
    assert adapter.actions.index("compact") < adapter.actions.index("unlock")
    assert workspace.load_json("active-checkpoint.json")["pending_compaction"] is None
    history = workspace.repository.record_history("checkpoints", result["checkpoint_id"])
    assert [record["revision"] for record in history] == [1]


def test_noop_compact_retry_is_runtime_only(workspace) -> None:
    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages(), worker=basic_worker)
    engine = CheckpointEngine(workspace, adapter)
    first = engine.request("manual", intent="compact")
    head = workspace.repository.head()
    checkpoint_count = len(list(workspace.repository.iter_records("checkpoints")))
    worker_calls = adapter.worker_calls
    transaction_path = workspace.config.memory_root / "transactions" / "transactions.jsonl"
    transaction_count = len(transaction_path.read_text(encoding="utf-8").splitlines())

    adapter.failures["compact"] = RuntimeError("native compact failed")
    with pytest.raises(RuntimeError):
        engine.request("manual", intent="compact")
    failed = engine._load()
    assert failed.consolidation_status == "no_op"
    assert failed.compaction_status == "failed"
    assert workspace.repository.head() == head
    assert len(list(workspace.repository.iter_records("checkpoints"))) == checkpoint_count

    retried = engine.retry()
    assert retried["ok"]
    assert retried["receipt"]["canonical_transaction"]["created"] is False
    assert adapter.worker_calls == worker_calls
    assert workspace.repository.head() == head
    assert len(list(workspace.repository.iter_records("checkpoints"))) == checkpoint_count
    assert len(transaction_path.read_text(encoding="utf-8").splitlines()) == transaction_count


def test_final_receipt_generation_retries_without_duplicate_compact(workspace) -> None:
    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages(), worker=basic_worker)
    adapter.failures["publish_receipt"] = RuntimeError("Host receipt unavailable")
    engine = CheckpointEngine(workspace, adapter)
    with pytest.raises(RuntimeError):
        engine.request("manual")

    failed = engine._load()
    assert failed.pending_acceptance == "closed"
    assert failed.receipt_generation == 1
    assert failed.host_receipt_generation is None
    assert failed.input_unlocked is False
    assert adapter.compact_calls == 1

    retried = engine.retry()
    assert retried["receipt"]["receipt_generation"] == 1
    assert retried["receipt"]["host_receipt_generation"] == 1
    assert retried["checkpoint"]["input_unlocked"] is True
    assert adapter.compact_calls == 1
    assert len(adapter.receipts) == 1


def test_compaction_cursor_is_independent_and_survives_restart(workspace) -> None:
    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages(), worker=basic_worker)
    engine = CheckpointEngine(workspace, adapter)

    consolidated = engine.request("manual", intent="consolidate")
    assert consolidated["receipt"]["consolidation_cursor_after"] == "msg_assistant_1"
    assert workspace.thread().last_consolidated_message_id == "msg_assistant_1"
    assert workspace.thread().compaction_cursor is None

    # Simulate a previously confirmed compact boundary before the latest
    # consolidate. The next compact must use this independent baseline, not
    # the consolidation cursor that now points at assistant_1.
    thread = workspace.thread()
    thread.compaction_cursor = "msg_user_1"
    workspace.save_thread(thread)
    adapter.worker = followup_worker
    adapter.messages.extend([
        {
            "id": "msg_user_2",
            "native_message_id": "native_user_2",
            "role": "user",
            "kind": "conversation",
            "content": "继续补充一个新的公开边界。",
            "created_at": NOW,
        },
        {
            "id": "msg_assistant_2",
            "native_message_id": "native_assistant_2",
            "role": "assistant",
            "kind": "conversation",
            "content": "收到新的公开材料。",
            "created_at": NOW,
        },
    ])

    compacted = engine.request("manual", intent="compact")
    assert compacted["receipt"]["compaction_cursor_before"] == "msg_user_1"
    assert compacted["receipt"]["compaction_cursor_after"] == "msg_assistant_2"
    assert workspace.thread().compaction_cursor == "msg_assistant_2"
    assert workspace.thread().last_consolidated_message_id == "msg_assistant_2"

    # A new engine instance must initialize the next operation from the
    # durable thread cursor rather than from last_consolidated_message_id or
    # an in-memory checkpoint object.
    reopened = Workspace(workspace.config)
    reopened.refresh_repository_profile()
    restarted = CheckpointEngine(reopened, adapter)
    next_compact = restarted.request("manual", intent="compact")
    assert next_compact["receipt"]["consolidation"]["status"] == "no_op"
    assert next_compact["receipt"]["compaction_cursor_before"] == "msg_assistant_2"
    assert reopened.thread().compaction_cursor == "msg_assistant_2"


def test_legacy_thread_without_compaction_cursor_remains_unknown(workspace) -> None:
    legacy = workspace.load_json("thread.json")
    legacy.pop("compaction_cursor", None)
    legacy["last_consolidated_message_id"] = "msg_assistant_1"
    workspace.save_json("thread.json", legacy)

    thread = workspace.thread()
    assert thread.last_consolidated_message_id == "msg_assistant_1"
    assert thread.compaction_cursor is None

    legacy_checkpoint = {
        "id": "ckpt_legacy",
        "trigger": "manual",
        "status": "DONE",
        "after_message_id": None,
        "through_message_id": "msg_assistant_1",
        "thread_id": thread.thread_id,
        "harness_binding_id": workspace.binding().id,
        "parent_session_id": "ses_fake_main",
        "compacted": True,
    }
    migrated = CheckpointState.model_validate(legacy_checkpoint)
    assert migrated.compaction_status == "completed"
    assert migrated.compaction_cursor_after is None


def test_failed_compact_does_not_advance_thread_cursor_until_retry_succeeds(workspace) -> None:
    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages(), worker=basic_worker)
    engine = CheckpointEngine(workspace, adapter)
    first = engine.request("manual", intent="compact")
    assert workspace.thread().compaction_cursor == "msg_assistant_1"

    adapter.worker = followup_worker
    adapter.messages.extend([
        {
            "id": "msg_user_2",
            "native_message_id": "native_user_2",
            "role": "user",
            "kind": "conversation",
            "content": "新的公开材料。",
            "created_at": NOW,
        },
        {
            "id": "msg_assistant_2",
            "native_message_id": "native_assistant_2",
            "role": "assistant",
            "kind": "conversation",
            "content": "记录新的边界。",
            "created_at": NOW,
        },
    ])
    adapter.failures["compact"] = RuntimeError("native compact failed")
    with pytest.raises(RuntimeError):
        engine.request("manual", intent="compact")
    assert workspace.thread().compaction_cursor == "msg_assistant_1"
    failed = engine._load()
    assert failed.compaction_cursor_before == "msg_assistant_1"
    assert failed.compaction_cursor_after is None

    retried = engine.retry()
    assert retried["ok"]
    assert workspace.thread().compaction_cursor == "msg_assistant_2"
    assert adapter.compact_calls == 2  # first success and retry success


def test_meta_proposal_cannot_commit_until_yes(workspace) -> None:
    def worker(_payload) -> WorkerResult:
        return WorkerResult(
            operations=[
                Operation(op="append", stream="hypotheses", record=hypothesis()),
                Operation(op="append", stream="meta_revisions", record=meta()),
                Operation(op="append", stream="continuations", record=continuation()),
            ]
        )

    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages(), worker=worker)
    engine = CheckpointEngine(workspace, adapter)
    pending = engine.request("manual")
    assert pending["status"] == "AWAITING_META_APPROVAL"
    assert adapter.compact_calls == 0
    assert not workspace.repository.current_records("meta_revisions")
    reviewed = [
        Operation.model_validate(operation)
        for operation in pending["proposal"]["operations"]
        if operation.get("stream") == "meta_revisions"
    ]
    assert proposal_hash(reviewed) == pending["proposal"]["proposal_hash"]
    assert pending["proposal"]["main_evidence"]
    assert any("拖延" in str(item.get("content", "")) for item in pending["proposal"]["main_evidence"])

    with pytest.raises(MemError) as error:
        engine.decide("yes")
    assert error.value.detail.code == "APPROVAL_PROVENANCE_REQUIRED"
    assert workspace.repository.current_records("meta_revisions") == {}

    result = engine.decide(
        "yes",
        question_request_id="question_test",
        approval_grant=approval_grant(pending["proposal"]),
        session_id=adapter.session_id,
    )
    assert result["ok"]
    assert adapter.resume_calls == 1
    assert result["receipt"]["proposal_hash"] == pending["proposal"]["proposal_hash"]
    # The checkpoint-result record now lands in its own post-commit transaction,
    # so the main changeset is byte-identical between the reviewed proposal and
    # the committed transaction.
    assert result["receipt"]["transaction_proposal_hash"] == pending["proposal"]["transaction_proposal_hash"]
    assert workspace.repository.current_records("meta_revisions")["meta_current"]
    transactions = (workspace.config.memory_root / "transactions" / "transactions.jsonl").read_text(encoding="utf-8").splitlines()
    transaction = next(
        line
        for line in reversed(transactions)
        if f'"transaction_proposal_hash": "{result["receipt"]["transaction_proposal_hash"]}"' in line
    )
    assert '"decision": "yes"' in transaction
    assert '"transaction_proposal_hash"' in transaction


def test_no_requires_reason_and_reuses_worker_once(workspace) -> None:
    def worker(payload) -> WorkerResult:
        if payload["kind"] == "consolidate":
            return WorkerResult(
                operations=[
                    Operation(op="append", stream="hypotheses", record=hypothesis()),
                    Operation(op="append", stream="meta_revisions", record=meta()),
                    Operation(op="append", stream="continuations", record=continuation()),
                ]
            )
        rejected = hypothesis(status="rejected")
        rejected["payload"]["counter_evidence_refs"] = [f"message:{payload['decision_message_id']}"]
        rejected["payload"]["revision_reason"] = payload["reason"]
        return WorkerResult(
            operations=[
                Operation(op="append", stream="hypotheses", record=rejected),
                Operation(op="append", stream="continuations", record=continuation(through=payload["decision_message_id"])),
            ]
        )

    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages(), worker=worker)
    engine = CheckpointEngine(workspace, adapter)
    engine.request("manual")
    with pytest.raises(MemError) as error:
        engine.decide("no", reason=" ")
    assert error.value.detail.code == "REJECTION_REASON_REQUIRED"

    pending = engine.status()["proposal"]
    result = engine.decide(
        "no",
        reason="更准确的是厌恶被评价，而不是害怕失败。",
        question_request_id="question_rejection_1",
        approval_grant=approval_grant(pending, decision="no", question_request_id="question_rejection_1", reason="更准确的是厌恶被评价，而不是害怕失败。"),
        session_id=adapter.session_id,
    )
    assert result["ok"]
    assert result["receipt"]["promotion_proposal"] is True
    assert result["receipt"]["approval_decision"] == "no"
    assert result["receipt"]["protected_streams"] == ["meta_revisions"]
    checkpoint_id = result["checkpoint_id"]
    initial = workspace.load_json(f"checkpoints/{checkpoint_id}/proposal-initial.json")
    revised = workspace.load_json(f"checkpoints/{checkpoint_id}/proposal-revised.json")
    assert initial["protected_streams"] == ["meta_revisions"]
    assert revised["protected_streams"] == []
    assert adapter.resume_calls == 2
    assert not workspace.repository.current_records("meta_revisions")
    current_hypothesis = workspace.repository.current_records("hypotheses")["hyp_evaluation"]
    assert current_hypothesis["payload"]["status"] == "rejected"
    decisions = [record for record in workspace.repository.iter_records("messages") if record["payload"]["kind"] == "checkpoint_decision"]
    assert len(decisions) == 1


def test_rejection_requires_disputed_hypothesis_with_decision_evidence(workspace) -> None:
    def worker(payload) -> WorkerResult:
        if payload["kind"] == "consolidate":
            return WorkerResult(
                operations=[
                    Operation(op="append", stream="hypotheses", record=hypothesis()),
                    Operation(op="append", stream="meta_revisions", record=meta()),
                    Operation(op="append", stream="continuations", record=continuation()),
                ]
            )
        return WorkerResult(
            operations=[
                Operation(
                    op="append",
                    stream="continuations",
                    record=continuation(through=payload["decision_message_id"]),
                )
            ]
        )

    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages(), worker=worker)
    engine = CheckpointEngine(workspace, adapter)
    engine.request("manual")
    pending = engine.status()["proposal"]
    with pytest.raises(MemError) as error:
        engine.decide(
            "no",
            reason="这个解释不准确。",
            question_request_id="question_rejection_2",
            approval_grant=approval_grant(pending, decision="no", question_request_id="question_rejection_2", reason="这个解释不准确。"),
            session_id=adapter.session_id,
        )
    assert error.value.detail.code == "REJECTION_HYPOTHESIS_REVISION_REQUIRED"
    with pytest.raises(MemError) as retry_error:
        engine.retry()
    assert retry_error.value.detail.code == "REJECTION_HYPOTHESIS_REVISION_REQUIRED"
    assert not workspace.repository.current_records("meta_revisions")


def test_rejection_reuses_initial_wrapper_search_receipts(workspace) -> None:
    concept = envelope(
        "psy_rejection_receipt",
        "pco/psychology/v1",
        {
            "name": "评价顾虑",
            "description": "对外部评价的持续顾虑。",
            "aliases": [],
            "external_refs": [
                {
                    "url": "https://example.org/evaluation",
                    "title": "Evaluation reference",
                    "accessed_at": NOW,
                    "search_receipt": "worker_placeholder",
                }
            ],
            "status": "active",
        },
    )
    receipt = envelope(
        "search_rejection_receipt",
        "pco/search-receipt/v1",
        {
            "worker_session_id": "ses_rejection_worker",
            "call_id": "call_rejection_search",
            "tool": "websearch",
            "input": {"query": "evaluation"},
            "output_excerpt": "Result: https://example.org/evaluation",
            "status": "completed",
        },
    )

    def worker(payload) -> WorkerResult:
        if payload["kind"] == "consolidate":
            return WorkerResult(
                operations=[
                    Operation(op="append", stream="psychologies", record=concept),
                    Operation(op="append", stream="hypotheses", record=hypothesis()),
                    Operation(op="append", stream="meta_revisions", record=meta()),
                    Operation(op="append", stream="continuations", record=continuation()),
                ],
                search_receipts=[receipt],
            )
        rejected = hypothesis(status="rejected")
        rejected["payload"]["counter_evidence_refs"] = [f"message:{payload['decision_message_id']}"]
        rejected["payload"]["revision_reason"] = payload.get("reason", "用户否定了初始解释。")
        return WorkerResult(
            operations=[
                Operation(op="append", stream="psychologies", record=concept),
                Operation(op="append", stream="hypotheses", record=rejected),
                Operation(op="append", stream="continuations", record=continuation(through=payload["decision_message_id"])),
            ]
        )

    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages(), worker=worker)
    engine = CheckpointEngine(workspace, adapter)
    assert engine.request("manual")["approval_required"]
    pending = engine.status()["proposal"]
    result = engine.decide(
        "no",
        reason="用户否定了初始解释。",
        question_request_id="question_rejection_3",
        approval_grant=approval_grant(pending, decision="no", question_request_id="question_rejection_3", reason="用户否定了初始解释。"),
        session_id=adapter.session_id,
    )
    assert result["ok"]
    stored_receipt = workspace.repository.current_records("search_receipts")[receipt["id"]]
    stored_concept = workspace.repository.current_records("psychologies")[concept["id"]]
    assert stored_receipt["payload"]["call_id"] == "call_rejection_search"
    assert stored_concept["payload"]["external_refs"][0]["search_receipt"] == receipt["id"]


@pytest.mark.parametrize(
    "operation",
    [
        Operation(op="append", stream="messages", record={"id": "forged"}),
        Operation(op="append", stream="sources", record={"id": "forged"}),
        Operation(op="append", stream="checkpoints", record={"id": "forged"}),
        Operation(op="write_artifact", path="sources/snapshots/forged.txt", content="forged"),
    ],
)
def test_worker_cannot_write_wrapper_managed_operations(workspace, operation: Operation) -> None:
    adapter = FakeHarnessAdapter(
        workspace.config.state_root,
        messages=visible_messages(),
        worker=lambda _payload: WorkerResult(
            operations=[operation, Operation(op="append", stream="continuations", record=continuation())]
        ),
    )
    with pytest.raises(MemError) as error:
        CheckpointEngine(workspace, adapter).request("manual")
    assert error.value.detail.code == "WORKER_OPERATION_NOT_ALLOWED"


def test_continuation_profile_token_limit_is_enforced(workspace) -> None:
    oversized = continuation()
    oversized["payload"]["current_topics"] = ["界" * 1300]
    adapter = FakeHarnessAdapter(
        workspace.config.state_root,
        messages=visible_messages(),
        worker=lambda _payload: WorkerResult(
            operations=[Operation(op="append", stream="continuations", record=oversized)]
        ),
    )
    with pytest.raises(MemError) as error:
        CheckpointEngine(workspace, adapter).request("manual")
    assert error.value.detail.code == "CONTINUATION_TOO_LONG"


def test_validate_failure_blocks_compact_and_retry_keeps_boundary(workspace) -> None:
    calls = 0

    def worker(_payload) -> WorkerResult:
        nonlocal calls
        calls += 1
        record = event()
        if calls == 1:
            record["payload"]["links"]["psychologies"] = ["psy_missing"]
        return WorkerResult(
            operations=[
                Operation(op="append", stream="events", record=record),
                Operation(op="append", stream="continuations", record=continuation()),
            ]
        )

    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages(), worker=worker)
    engine = CheckpointEngine(workspace, adapter)
    with pytest.raises(MemError) as error:
        engine.request("manual")
    assert error.value.detail.code == "REFERENCE_NOT_FOUND"
    assert adapter.compact_calls == 0
    assert workspace.thread().last_consolidated_message_id is None
    frozen = workspace.load_json(f"checkpoints/{engine._load().id}/frozen.json")

    result = engine.retry()
    assert result["ok"]
    assert adapter.compact_calls == 1
    assert frozen["message_range"] == result["receipt"]["message_range"]


def test_context_publish_failure_retries_without_recommit(workspace) -> None:
    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages(), worker=basic_worker)
    adapter.failures["publish_context"] = RuntimeError("publication failed")
    engine = CheckpointEngine(workspace, adapter)
    with pytest.raises(RuntimeError):
        engine.request("manual")
    state = engine._load()
    assert state.commit is not None
    committed_head = workspace.repository.head()
    assert adapter.compact_calls == 0

    result = engine.retry()
    assert result["ok"]
    assert result["receipt"]["git_commit"] == committed_head
    assert adapter.compact_calls == 1


def test_worker_cleanup_failure_is_a_retryable_derivation(workspace) -> None:
    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages(), worker=basic_worker)
    adapter.failures["close_worker"] = RuntimeError("worker delete failed")
    engine = CheckpointEngine(workspace, adapter)
    result = engine.request("manual")
    assert result["status"] == "COMMITTED_WITH_PENDING_DERIVATIONS"
    assert result["receipt"]["derivations"]["worker_cleanup"]["pending"]

    retried = engine.retry_derivations()
    assert retried["status"] == "DONE"
    assert retried["derivations"]["worker_cleanup"]["ok"]
    persisted_receipt = workspace.load_json(f"checkpoints/{retried['checkpoint_id']}/receipt.json")
    assert persisted_receipt["status"] == "DONE"
    assert persisted_receipt["derivations"]["worker_cleanup"]["ok"] is True
    assert persisted_receipt["derivations"]["worker_cleanup"]["pending"] is False
    assert persisted_receipt["receipt_generation"] == 2
    assert persisted_receipt["receipt_delivery"]["host_disposition"] == "created"
    assert len(adapter.receipt_resources) == 2
    history = workspace.repository.record_history("checkpoints", retried["checkpoint_id"])
    # Cleanup is runtime-only; its retry must not revise the consolidation
    # canonical record or create another Git commit.
    assert [record["revision"] for record in history] == [1]
    assert history[0]["payload"]["status"] == "committed"
    assert "worker_cleanup" not in history[0]["payload"]["derivations"]

    # A retry after the successful revision is idempotent and must not append
    # another canonical checkpoint revision.
    retried_again = engine.retry_derivations()
    assert retried_again["status"] == "DONE"
    assert [record["revision"] for record in workspace.repository.record_history("checkpoints", retried["checkpoint_id"])] == [1]


def test_unavailable_child_is_rebuilt_from_the_same_frozen_boundary(workspace) -> None:
    calls = 0

    def worker(_payload) -> WorkerResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MemError("WORKER_INTERRUPTED", "worker", "temporary worker interruption", retryable=True)
        if calls == 2:
            raise MemError("HARNESS_REQUEST_FAILED", "harness", "child session no longer exists", retryable=True)
        return basic_worker(_payload)

    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages(), worker=worker)
    engine = CheckpointEngine(workspace, adapter)
    with pytest.raises(MemError):
        engine.request("manual")
    original = engine._load().worker_handle
    frozen = workspace.load_json(f"checkpoints/{engine._load().id}/frozen.json")

    result = engine.retry()
    assert result["status"] == "DONE"
    assert adapter.worker_calls == 2
    assert engine._load().worker_handle != original
    assert result["receipt"]["message_range"] == frozen["message_range"]


def test_checkpoint_record_carries_real_commit_and_derivations(workspace) -> None:
    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages(), worker=basic_worker)
    result = CheckpointEngine(workspace, adapter).request("manual")
    record = workspace.repository.current_records("checkpoints")[result["checkpoint_id"]]
    # Cleanup is completed before the canonical final snapshot, so one record
    # already carries the same outcome as the receipt.
    assert record["revision"] == 1
    assert record["payload"]["git_commit"] == result["receipt"]["git_commit"]
    assert record["payload"]["audit_transaction_id"] == result["receipt"]["audit_transaction_id"]
    assert record["payload"]["status"] in {"committed", "committed_with_pending_derivations"}
    assert record["payload"]["requested_intent"] == "compact"
    for runtime_field in {
        "compaction_requested", "compaction_status", "compaction_origin",
        "pending_compaction", "pending_acceptance", "native_compact_attempt_id",
        "compaction_cursor_before", "compaction_cursor_after",
        "receipt_generation", "host_receipt_generation", "receipt_kind", "receipt_key",
    }:
        assert runtime_field not in record["payload"]
    assert record["payload"]["derivations"] != {
        "index": "scheduled",
        "backlinks": "scheduled",
        "projection": "scheduled",
    }


def test_idempotent_audit_write_recovers_missing_runtime_provenance(workspace) -> None:
    adapter = FakeHarnessAdapter(workspace.config.state_root, messages=visible_messages(), worker=basic_worker)
    engine = CheckpointEngine(workspace, adapter)
    result = engine.request("manual")
    checkpoint_id = result["checkpoint_id"]
    expected_audit_commit = result["receipt"]["audit_commit"]
    expected_audit_transaction = result["receipt"]["audit_transaction_id"]

    persisted = workspace.load_json("active-checkpoint.json")
    persisted["audit_commit"] = None
    persisted["audit_transaction_id"] = None
    workspace.save_json("active-checkpoint.json", persisted)
    state = engine._load()
    assert state.audit_commit is None
    assert state.audit_transaction_id is None

    assert finalize_steps.write_checkpoint_record(engine, state) == expected_audit_commit
    restored = engine._load()
    assert restored.audit_commit == expected_audit_commit
    assert restored.audit_transaction_id == expected_audit_transaction
    assert finalize_steps.receipt(engine, restored)["audit_commit"] == expected_audit_commit
