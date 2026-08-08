from __future__ import annotations

import uuid
from typing import Any

from mem_core.errors import MemError, ensure
from mem_core.models import utc_now

from ..harness import WorkerHandle
from . import derivations as derivations_steps
from . import finalize as finalize_steps
from . import state as state_store
from . import steps as checkpoint_steps


def retry(engine: Any) -> dict[str, Any]:
    state = state_store.load(engine)
    ensure(
        state.status in {"RECOVERY", "COMMITTED_CONTEXT_PENDING", "RECEIPT_INSERTED", "INPUT_UNLOCKED", "DERIVATIONS_RUNNING"},
        "CHECKPOINT_NOT_RETRYABLE",
        "checkpoint",
        f"Checkpoint is {state.status}",
    )
    state.retries += 1
    state.error = None
    state.failure_phase = None
    if state.commit:
        state_store.transition(engine, state, "MEMORY_COMMITTED")
        return finalize_steps.finalize_committed(engine, state)
    try:
        frozen = engine.workspace.load_json(f"checkpoints/{state.id}/frozen.json")
        handle = WorkerHandle(**(state.worker_handle or {}))
        payload: dict[str, Any]
        if state.decision == "no":
            payload = {
                "kind": "rejection_revision",
                "decision_message_id": state.decision_message_id,
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
    ensure(state.commit is not None, "CHECKPOINT_NOT_COMMITTED", "derivations", "Checkpoint has no canonical commit")
    if not state.input_unlocked:
        engine.adapter.unlock_input()
        state.input_unlocked = True
    derivations_steps.run_derivations(engine, state)
    derivations_steps.cleanup_worker(engine, state)
    pending = any(not item.get("ok", False) for item in state.derivations.values())
    state.completed_at = utc_now()
    state_store.transition(engine, state, "COMMITTED_WITH_PENDING_DERIVATIONS" if pending else "DONE")
    finalize_steps.write_checkpoint_record(engine, state)
    current_receipt = finalize_steps.receipt(engine, state)
    engine.workspace.save_json(f"checkpoints/{state.id}/receipt.json", current_receipt)
    return {"ok": True, "checkpoint_id": state.id, "status": state.status, "derivations": state.derivations, "receipt": current_receipt}


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
