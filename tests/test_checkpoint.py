from __future__ import annotations

import pytest

from mem_core.errors import MemError
from mem_core.models import Operation, proposal_hash
from pco.checkpoint import CheckpointEngine
from pco.harness import FakeHarnessAdapter, WorkerResult

from conftest import continuation, event, hypothesis, meta, visible_messages


def basic_worker(_payload) -> WorkerResult:
    return WorkerResult(
        operations=[
            Operation(op="append", stream="events", record=event()),
            Operation(op="append", stream="hypotheses", record=hypothesis()),
            Operation(op="append", stream="continuations", record=continuation()),
        ],
        skill_versions={"pco-consolidate": "0.1.0"},
    )


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
    assert adapter.actions.index("unlock") < adapter.actions.index("close_worker")
    assert workspace.thread().last_consolidated_message_id == "msg_assistant_1"
    assert workspace.repository.current_records("events")["evt_publish_delay"]
    frozen = workspace.load_json(f"checkpoints/{result['checkpoint_id']}/frozen.json")
    contract = frozen["profile_contract"]
    assert contract["allowed_streams"]["continuations"]["write_policy"] == "auto"
    assert contract["allowed_streams"]["continuations"]["record_schema"]["required"]
    assert "messages" not in contract["allowed_streams"]


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

    result = engine.decide("yes")
    assert result["ok"]
    assert adapter.resume_calls == 1
    assert result["receipt"]["proposal_hash"] == pending["proposal"]["proposal_hash"]
    assert result["receipt"]["transaction_proposal_hash"] != pending["proposal"]["transaction_proposal_hash"]
    assert workspace.repository.current_records("meta_revisions")["meta_current"]
    transaction = list((workspace.config.memory_root / "transactions" / "transactions.jsonl").read_text(encoding="utf-8").splitlines())[-1]
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

    result = engine.decide("no", reason="更准确的是厌恶被评价，而不是害怕失败。")
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
    with pytest.raises(MemError) as error:
        engine.decide("no", reason="这个解释不准确。")
    assert error.value.detail.code == "REJECTION_HYPOTHESIS_REVISION_REQUIRED"
    assert not workspace.repository.current_records("meta_revisions")


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
    assert workspace.repository.head() == committed_head
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
