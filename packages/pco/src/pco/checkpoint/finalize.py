from __future__ import annotations

import json
import uuid
from collections import Counter
from typing import Any

from mem_core.errors import ensure
from mem_core.models import Operation, utc_now
from mem_core.transaction import TransactionManager

from ..context import render as render_context
from . import derivations as derivations_steps
from . import state as state_store
from .state import CheckpointState


def commit_and_finalize(engine: Any, state: CheckpointState) -> dict[str, Any]:
    ensure(state.transaction_id is not None, "CHECKPOINT_STATE_INVALID", "commit", "Missing transaction")
    result = engine.manager.commit(state.transaction_id)
    state.commit = result["commit"]
    thread = engine.workspace.thread()
    thread.last_consolidated_message_id = (
        state.decision_message_id
        if state.decision == "yes" and state.decision_message_id
        else state.through_message_id
    )
    engine.workspace.save_thread(thread)
    state_store.transition(engine, state, "MEMORY_COMMITTED")
    return finalize_committed(engine, state)


def receipt(engine: Any, state: CheckpointState) -> dict[str, Any]:
    workspace = engine.workspace
    proposal = workspace.load_json(f"checkpoints/{state.id}/proposal.json")
    counts = Counter(
        operation.get("stream", "artifacts")
        for operation in proposal["operations"]
        if operation.get("stream") != "checkpoints"
    )
    meta_updated = counts.get("meta_revisions", 0) > 0
    approval = state.decision or ("not_required" if not state.protected_streams else None)
    summary = (
        f"记忆 checkpoint 完成：事件 {counts.get('events', 0)}，hypothesis {counts.get('hypotheses', 0)}；"
        f"Meta-memory {'已更新' if meta_updated else '未更新'}（{approval}）；Git {state.commit[:8] if state.commit else 'pending'}"
    )
    promotion_hash = state.promotion_proposal_hash or state.proposal_hash
    promotion_streams = state.promotion_protected_streams or state.protected_streams
    return {
        "ok": True,
        "checkpoint_id": state.id,
        "trigger": state.trigger,
        "status": state.status,
        "started_at": state.created_at,
        "completed_at": state.completed_at,
        "thread_id": state.thread_id,
        "harness_binding_id": state.harness_binding_id,
        "parent_session_id": state.parent_session_id,
        "worker": state.worker_handle,
        "archive_cursor": state.archive_cursor,
        "message_range": {"after": state.after_message_id, "through": state.through_message_id},
        "source_hashes": state.source_hashes,
        "operation_counts": dict(counts),
        "promotion_proposal": bool(promotion_streams),
        "protected_streams": promotion_streams,
        "approval_decision": approval,
        "proposal_hash": promotion_hash,
        "final_proposal_hash": state.proposal_hash,
        "transaction_proposal_hash": state.transaction_proposal_hash,
        "transaction_fingerprint": state.transaction_fingerprint,
        "git_commit": state.commit,
        "meta_updated": meta_updated,
        "meta_revision": state.meta_revision,
        "continuation_updated": counts.get("continuations", 0) > 0,
        "continuation_revision": state.continuation_revision,
        "runtime": state.harness_runtime,
        "versions": {
            "profile": f"{workspace.profile.name}@{workspace.profile.version}",
            "policy_hash": workspace.profile.policy_hash,
            "workflow": "consolidate@0.3.1",
            "skills": state.skill_versions,
        },
        "retry_count": state.retries,
        "error": state.error,
        "derivations": state.derivations,
        "summary": summary,
    }


def _checkpoint_derivations(state: CheckpointState) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name, item in state.derivations.items():
        entry: dict[str, Any] = {"ok": bool(item.get("ok", False))}
        if not entry["ok"]:
            entry["pending"] = True
        if item.get("error"):
            error = item["error"]
            # Keep structured MemError details queryable in canonical memory
            # (for example code/recovery/pointer), while retaining a safe
            # fallback for arbitrary exception values.
            try:
                json.dumps(error, ensure_ascii=False)
            except (TypeError, ValueError):
                error = str(error)
            entry["error"] = error
        result[name] = entry
    return result


def _checkpoint_payload(engine: Any, state: CheckpointState) -> dict[str, Any]:
    workspace = engine.workspace
    derivations = _checkpoint_derivations(state)
    pending = any(not item["ok"] for item in derivations.values())
    binding = workspace.binding()
    approval = state.decision or ("not_required" if not state.protected_streams else "no")
    return {
        "thread_id": binding.thread_id,
        "harness_binding_id": binding.id,
        "parent_session_id": state.parent_session_id,
        "archive_cursor": state.archive_cursor,
        "source_hashes": state.source_hashes,
        "worker": state.worker_handle,
        "runtime": state.harness_runtime,
        "trigger": state.trigger,
        "status": "committed_with_pending_derivations" if pending else "committed",
        "message_range": {"after": state.after_message_id, "through": state.through_message_id},
        "transaction_id": state.transaction_id,
        "git_commit": state.commit,
        "operation_counts": dict(state.operation_counts),
        "proposal_hash": state.proposal_hash,
        "promotion_proposal_hash": state.promotion_proposal_hash,
        "approval_decision": approval,
        "protected_streams": state.protected_streams,
        "promotion_protected_streams": state.promotion_protected_streams,
        "meta_revision": state.meta_revision,
        "continuation_revision": state.continuation_revision,
        "derivations": derivations,
        "versions": {
            "profile": f"{workspace.profile.name}@{workspace.profile.version}",
            "policy_hash": workspace.profile.policy_hash,
            "workflow": "consolidate@0.3.1",
            "skills": state.skill_versions,
        },
        "warnings": [],
        "started_at": state.created_at,
        "ended_at": utc_now(),
        "retry_count": state.retries,
    }


def write_checkpoint_record(engine: Any, state: CheckpointState) -> None:
    workspace = engine.workspace
    current = workspace.repository.current_records("checkpoints").get(state.id)
    payload = _checkpoint_payload(engine, state)
    if current is not None:
        current_payload = current.get("payload", {})
        # Runtime timestamps and retry counters are not canonical state
        # changes by themselves. Only an observable checkpoint outcome or
        # derivation result warrants another append-only revision.
        if (
            current_payload.get("status") == payload["status"]
            and current_payload.get("derivations") == payload["derivations"]
        ):
            return
        revision = int(current["revision"]) + 1
    else:
        revision = 1
    record = {
        "id": state.id,
        "revision": revision,
        "recorded_at": utc_now(),
        "schema_version": "pco/checkpoint/v1",
        "payload": payload,
    }
    manager = TransactionManager(workspace.repository, workspace.config.state_root)
    txn = manager.begin(
        transaction_id=f"txn_checkpoint_{state.id[5:17]}_{uuid.uuid4().hex[:8]}",
        fingerprint_context={"kind": "checkpoint_result", "checkpoint_id": state.id},
    )
    manager.append(txn.id, Operation(op="append", stream="checkpoints", record=record))
    manager.commit(txn.id)


def finalize_committed(engine: Any, state: CheckpointState) -> dict[str, Any]:
    workspace = engine.workspace
    try:
        if state.context_bundle is None:
            state.context_bundle = render_context(
                repo_root=workspace.config.memory_root,
                output_path=workspace.config.state_root / "context" / "current.md",
                checkpoint_id=state.id,
            )
            state_store.save(engine, state)
        if not state.context_published:
            engine.adapter.publish_context(state.context_bundle)
            state.context_published = True
            state_store.transition(engine, state, "CONTEXT_PUBLISHED")
        if not state.compacted:
            engine.adapter.compact()
            state.compacted = True
            state_store.transition(engine, state, "CONTEXT_COMPACTED")
    except Exception as exc:
        state.status = "COMMITTED_CONTEXT_PENDING"
        state_store.recover(engine, state, exc, preserve_status=True)
        raise
    try:
        if not state.receipt_inserted:
            state_store.transition(engine, state, "RECEIPT_INSERTED")
            current_receipt = receipt(engine, state)
            engine.adapter.insert_receipt(current_receipt)
            state.receipt_inserted = True
            workspace.save_json(f"checkpoints/{state.id}/receipt.json", current_receipt)
        if not state.input_unlocked:
            engine.adapter.unlock_input()
            state.input_unlocked = True
            state_store.transition(engine, state, "INPUT_UNLOCKED")
    except Exception as exc:
        state.status = "COMMITTED_CONTEXT_PENDING"
        state_store.recover(engine, state, exc, preserve_status=True)
        raise
    state_store.transition(engine, state, "DERIVATIONS_RUNNING")
    derivations_steps.run_derivations(engine, state)
    derivations_steps.cleanup_worker(engine, state)
    pending = any(not item.get("ok", False) for item in state.derivations.values())
    state.completed_at = utc_now()
    state_store.transition(engine, state, "COMMITTED_WITH_PENDING_DERIVATIONS" if pending else "DONE")
    write_checkpoint_record(engine, state)
    current_receipt = receipt(engine, state)
    workspace.save_json(f"checkpoints/{state.id}/receipt.json", current_receipt)
    return {"ok": True, "checkpoint_id": state.id, "status": state.status, "receipt": current_receipt}
