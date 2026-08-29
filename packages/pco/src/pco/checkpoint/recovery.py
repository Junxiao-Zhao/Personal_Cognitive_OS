from __future__ import annotations

import uuid
from typing import Any

from mem_core.errors import MemError, ensure

from ..harness import WorkerHandle
from . import derivations as derivations_steps
from . import finalize as finalize_steps
from . import state as state_store
from . import steps as checkpoint_steps


def _retire_pending_if_complete(engine: Any, result: dict[str, Any]) -> dict[str, Any]:
    state = state_store.load(engine)
    if state.pending_compaction is not None and state.compaction_status == "completed" and state.receipt_inserted and state.input_unlocked:
        state_store.retire_pending_compaction(engine, state)
        refreshed = state_store.load(engine)
        result = dict(result)
        if isinstance(result.get("receipt"), dict):
            result["receipt"] = finalize_steps.receipt(engine, refreshed)
        result["pending_compaction"] = None
    return result


def resume_pending_compaction(engine: Any, result: dict[str, Any] | None = None) -> dict[str, Any]:
    """Continue the compact-only tail of a durable checkpoint.

    The original consolidate intent is preserved. This helper is deliberately
    called only after the existing finalizer has published context; it turns
    the pending request into the compact-only no-op phase and retires it only
    after receipt insertion and input unlock are durable.
    """

    state = state_store.load(engine)
    if state.pending_compaction is None:
        return result or {"ok": True, "checkpoint_id": state.id, "status": state.status}
    if state.context_publication_status != "completed" or not state.context_published or not state.content_commit:
        return result or {"ok": True, "checkpoint_id": state.id, "status": state.status, "pending_compaction": state.pending_compaction.model_dump(mode="json")}
    if state.compaction_status == "completed" and state.receipt_inserted and state.input_unlocked:
        return _retire_pending_if_complete(engine, result or {})

    if state.status in state_store.TERMINAL_STATUSES or state.input_unlocked:
        state.input_unlocked = False
        state.status = "COMMITTED_CONTEXT_PENDING"
        state_store.transition(engine, state, state.status)
    state.compaction_requested = True
    state.compaction_status = "pending" if state.compaction_status != "completed" else state.compaction_status
    state.compaction_origin = state.pending_compaction.origin
    state_store.save(engine, state)
    compact_result = finalize_steps.finalize_noop_compact(engine, state)
    return _retire_pending_if_complete(engine, compact_result)


def retry(engine: Any) -> dict[str, Any]:
    state = state_store.load(engine)
    receipt_pending = state.host_receipt_generation != state.receipt_generation or not state.input_unlocked
    ensure(
        state.status in {"RECOVERY", "COMMITTED_CONTEXT_PENDING", "RECEIPT_INSERTED", "INPUT_UNLOCKED", "DERIVATIONS_RUNNING", "CONTEXT_COMPACTED", "DONE", "COMMITTED_WITH_PENDING_DERIVATIONS"},
        # A terminal state is retryable only when the pending compact tail was
        # durably recorded before the process stopped.
        "CHECKPOINT_NOT_RETRYABLE",
        "checkpoint",
        f"Checkpoint is {state.status}",
    )
    # A committed checkpoint with only failed/pending derivations is a
    # derivation recovery operation, not a new checkpoint retry. Keep the
    # durable commit and retry only the replaceable derivation tail.
    if state.status == "COMMITTED_WITH_PENDING_DERIVATIONS" and state.pending_compaction is None:
        return retry_derivations(engine)
    ensure(
        state.status not in {"DONE", "COMMITTED_WITH_PENDING_DERIVATIONS"}
        or state.pending_compaction is not None
        or receipt_pending,
        "CHECKPOINT_NOT_RETRYABLE",
        "checkpoint",
        f"Checkpoint is {state.status}",
    )
    state.retries += 1
    state.error = None
    state.failure_phase = None
    if state.pending_compaction is not None and state.context_publication_status == "completed" and state.content_commit:
        return resume_pending_compaction(engine)
    if state.content_commit or state.commit:
        post_derivation = bool(
            state.context_published
            and (
                state.compaction_status in {"failed", "pending", "completed"}
                or state.failure_phase in {"NATIVE_COMPACT", "RECEIPT_INSERT", "RECEIPT_INSERTED", "INPUT_UNLOCKED"}
                or state.receipt_inserted
                or state.status in {"CONTEXT_COMPACTED", "RECEIPT_INSERTED", "INPUT_UNLOCKED", "DONE", "COMMITTED_WITH_PENDING_DERIVATIONS"}
            )
        )
        state_store.save(engine, state)
        if post_derivation:
            return _retire_pending_if_complete(engine, finalize_steps.finalize_after_derivations(engine, state))
        return resume_pending_compaction(engine, finalize_steps.finalize_committed(engine, state))
    try:
        frozen = engine.workspace.load_json(f"checkpoints/{state.id}/frozen.json")
        handle = WorkerHandle(**(state.worker_handle or {}))
        payload: dict[str, Any]
        if state.decision == "no":
            payload = {
                "kind": "rejection_revision",
                "decision_message_id": state.decision_message_id,
                "decision_question_request_id": state.decision_question_request_id,
                "decision_authorization_id": state.decision_authorization_id,
                "original_proposal_hash": state.proposal_hash,
                "requirements": ["remove all user_approval operations", "append a disputed/rejected hypothesis revision", "cite the archived decision as counter-evidence", "include a non-empty revision reason", "do not ask a follow-up question"],
                "frozen": frozen,
            }
        else:
            payload = {"kind": "consolidate", "frozen": frozen, "retry": state.retries}
        state_store.transition(engine, state, "WORKER_RUNNING")
        try:
            result = engine.adapter.resume_worker(handle, payload)
        except MemError as exc:
            if exc.detail.code != "HARNESS_REQUEST_FAILED":
                raise
            replacement = engine.adapter.spawn_worker(
                {
                    "checkpoint_id": state.id,
                    "worker_id": f"worker_{uuid.uuid4().hex}",
                    "replacement_for": handle.id,
                    "frozen_input_path": str(state_store.checkpoint_dir(engine, state.id) / "frozen.json"),
                }
            )
            state.worker_handle = replacement.as_dict()
            state_store.save(engine, state)
            result = engine.adapter.resume_worker(replacement, payload)
        return checkpoint_steps.prepare_candidate(engine, state, frozen, result)
    except Exception as exc:
        state_store.recover(engine, state, exc)
        raise


def retry_derivations(engine: Any) -> dict[str, Any]:
    state = state_store.load(engine)
    ensure(state.content_commit or state.commit is not None, "CHECKPOINT_NOT_COMMITTED", "derivations", "Checkpoint has no canonical commit")
    state.retries += 1
    # A derivation retry after a published final receipt is a new observable
    # runtime outcome. Reopen the operation under a higher receipt generation
    # so Host/UI state is superseded rather than silently changed on disk.
    if state.input_unlocked and state.host_receipt_generation == state.receipt_generation:
        state.receipt_generation += 1
        state.receipt_outbox = None
        state.receipt_inserted = False
        state.pending_acceptance = "open"
        state.input_unlocked = False
    state_store.save(engine, state)
    if not state.context_published:
        return finalize_steps.finalize_committed(engine, state)
    state_store.transition(engine, state, "DERIVATIONS_RUNNING")
    derivations_steps.run_derivations(engine, state)
    derivations_steps.cleanup_worker(engine, state)
    result = finalize_steps.finalize_after_derivations(engine, state)
    result["derivations"] = state.derivations
    return result


def abort(engine: Any) -> dict[str, Any]:
    state = state_store.load(engine)
    ensure(state.commit is None, "CHECKPOINT_ALREADY_COMMITTED", "checkpoint", "A committed checkpoint cannot be aborted; retry context publication")
    if state.transaction_id:
        transaction = engine.manager.load(state.transaction_id)
        if transaction.status != "aborted":
            engine.manager.abort(state.transaction_id)
    derivations_steps.cleanup_worker(engine, state)
    engine.adapter.unlock_input()
    state.input_unlocked = True
    state_store.transition(engine, state, "ABORTED")
    return {"ok": True, "checkpoint_id": state.id, "status": state.status}
