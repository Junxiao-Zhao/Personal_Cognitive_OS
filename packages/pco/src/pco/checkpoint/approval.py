from __future__ import annotations

import os
from typing import Any, Literal

from mem_core.errors import ensure
from mem_core.models import Operation

from ..harness import WorkerHandle, WorkerResult
from . import finalize as finalize_steps
from . import recovery as recovery_steps
from . import state as state_store
from . import steps as checkpoint_steps
from .authorization import consume as consume_grant
from .authorization import verify as verify_grant


def decide(
    engine: Any,
    decision: Literal["yes", "no"],
    *,
    reason: str | None = None,
    native_message_id: str | None = None,
    approval_grant: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    workspace = engine.workspace
    state = state_store.load(engine)
    ensure(state.status == "AWAITING_META_APPROVAL", "CHECKPOINT_NOT_AWAITING_APPROVAL", "approval", f"Checkpoint is {state.status}")
    ensure(state.transaction_id is not None and state.proposal_hash is not None, "CHECKPOINT_STATE_INVALID", "approval", "Missing candidate transaction")
    grant_payload: dict[str, Any] | None = None
    if decision == "yes":
        ensure(
            isinstance(approval_grant, str) and bool(approval_grant.strip()),
            "APPROVAL_PROVENANCE_REQUIRED",
            "approval",
            "Protected Meta approval requires a host-issued approval grant",
        )
        grant_payload = verify_grant(
            approval_grant,
            secret=os.environ.get("PCO_APPROVAL_GRANT_SECRET"),
            checkpoint_id=state.id,
            proposal_hash=state.proposal_hash,
            challenge_id=state.approval_challenge_id,
            session_id=session_id,
        )
        consume_grant(grant_payload, workspace.config.state_root)
        engine.manager.abort(state.transaction_id)
        state.decision = "yes"
        # Approval provenance belongs to the transaction receipt. It is not a
        # conversation turn and must not be synthesized as role=user content.
        state.decision_message_id = None
        frozen = workspace.load_json(f"checkpoints/{state.id}/frozen.json")
        frozen["base_commit"] = workspace.repository.head()
        workspace.save_json(f"checkpoints/{state.id}/frozen.json", frozen)
        persisted_result = workspace.load_json(f"checkpoints/{state.id}/worker-result-initial.json")
        checkpoint_steps.prepare_candidate(
            engine,
            state,
            frozen,
            WorkerResult(
                operations=[Operation.model_validate(item) for item in persisted_result["operations"]],
                search_receipts=persisted_result.get("search_receipts", []),
                diagnostics=persisted_result.get("diagnostics", []),
                skill_versions=persisted_result.get("skill_versions", {}),
                runtime_info=persisted_result.get("runtime_info", {}),
            ),
        )
        state = state_store.load(engine)
        ensure(state.transaction_id is not None, "CHECKPOINT_STATE_INVALID", "approval", "Rebuilt approval transaction is missing")
        engine.manager.attach_approval(
            state.transaction_id,
            checkpoint_id=state.id,
            proposal_hash_value=state.proposal_hash or "",
            decision_message_id=native_message_id,
            receipt_id=f"approval_{state.id}",
            authorization_id=grant_payload.get("grant_id") if grant_payload else None,
            authorization_source="opencode_command" if grant_payload else None,
        )
        state_store.transition(engine, state, "FINAL_CHANGESET_VALIDATED")
        try:
            return finalize_steps.commit_and_finalize(engine, state)
        except Exception as exc:
            state_store.recover(engine, state, exc)
            raise
    ensure(reason is not None and reason.strip(), "REJECTION_REASON_REQUIRED", "approval", "No requires a reason or supplemental experience", path="/reason")
    try:
        decision_record = engine.archive.archive_decision(
            checkpoint_id=state.id,
            proposal_hash=state.proposal_hash,
            decision="no",
            reason=reason,
            native_message_id=native_message_id,
        )
        state.decision = "no"
        state.decision_message_id = decision_record["message_id"]
        state.archive_cursor = workspace.thread().archive_cursor
        state.through_message_id = decision_record["message_id"]
        engine.manager.abort(state.transaction_id)
        frozen = workspace.load_json(f"checkpoints/{state.id}/frozen.json")
        frozen["message_range"]["through"] = state.through_message_id
        decision_message = workspace.repository.current_records("messages").get(state.decision_message_id)
        ensure(decision_message is not None, "DECISION_MESSAGE_NOT_ARCHIVED", "approval", "The rejection decision was not found in canonical conversation")
        frozen["messages"].append(decision_message)
        frozen["base_commit"] = workspace.repository.head()
        workspace.save_json(f"checkpoints/{state.id}/frozen.json", frozen)
        state_store.transition(engine, state, "WORKER_RUNNING")
        handle = WorkerHandle(**(state.worker_handle or {}))
        revised = engine.adapter.resume_worker(
            handle,
            {
                "kind": "rejection_revision",
                "approval_decision_id": f"decision_{state.id}",
                "decision_message_id": state.decision_message_id,
                "reason": reason.strip(),
                "original_proposal_hash": state.proposal_hash,
                "requirements": ["remove all user_approval operations", "append a disputed/rejected hypothesis revision", "extract supplemental event evidence when applicable", "do not ask a follow-up question"],
                "frozen": frozen,
            },
        )
        return checkpoint_steps.prepare_candidate(engine, state, frozen, revised)
    except Exception as exc:
        state_store.recover(engine, state, exc)
        raise
