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
    state.content_commit = result["commit"]
    state.commit = state.content_commit
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
        "content_commit": state.content_commit or state.commit,
        "derivation_source_commit": state.content_commit or state.commit,
        "audit_commit": state.audit_commit,
        "audit_transaction_id": state.audit_transaction_id,
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


def _checkpoint_payload(engine: Any, state: CheckpointState, *, audit_transaction_id: str | None = None) -> dict[str, Any]:
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
        "audit_transaction_id": audit_transaction_id or state.audit_transaction_id,
        "git_commit": state.commit,
        "content_commit": state.content_commit or state.commit,
        "derivation_source_commit": state.content_commit or state.commit,
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


def _restore_audit_provenance(engine: Any, state: CheckpointState, payload: dict[str, Any]) -> str:
    """Recover audit metadata after a commit-before-state-save crash."""

    audit_transaction_id = state.audit_transaction_id or payload.get("audit_transaction_id")
    ensure(
        isinstance(audit_transaction_id, str) and audit_transaction_id,
        "CHECKPOINT_AUDIT_PROVENANCE_MISSING",
        "checkpoint",
        "Existing checkpoint record has no audit transaction ID",
    )
    audit_commit = state.audit_commit or payload.get("audit_commit")
    if not audit_commit:
        manager = TransactionManager(engine.workspace.repository, engine.workspace.config.state_root)
        try:
            audit_commit = manager.load(audit_transaction_id).commit
        except Exception:
            # The transaction state is normally durable before manager.commit
            # returns. Keep a Git-log fallback for recovery from a partially
            # restored state directory.
            audit_commit = engine.workspace.repository._git(
                "log",
                "--all",
                "--format=%H",
                "--fixed-strings",
                "--grep",
                f"memory transaction {audit_transaction_id}",
                "-1",
            )
    ensure(
        isinstance(audit_commit, str) and audit_commit,
        "CHECKPOINT_AUDIT_PROVENANCE_MISSING",
        "checkpoint",
        "Existing audit transaction has no commit hash",
    )
    state.audit_transaction_id = audit_transaction_id
    state.audit_commit = audit_commit
    state_store.save(engine, state)
    return audit_commit


def write_checkpoint_record(engine: Any, state: CheckpointState) -> str | None:
    workspace = engine.workspace
    current = workspace.repository.current_records("checkpoints").get(state.id)
    audit_transaction_id = f"txn_checkpoint_{state.id[5:17]}_{uuid.uuid4().hex[:8]}"
    payload = _checkpoint_payload(engine, state, audit_transaction_id=audit_transaction_id)
    if current is not None:
        current_payload = current.get("payload", {})
        # Runtime timestamps and retry counters are not canonical state
        # changes by themselves. Only an observable checkpoint outcome or
        # derivation result warrants another append-only revision.
        if (
            current_payload.get("status") == payload["status"]
            and current_payload.get("derivations") == payload["derivations"]
            and current_payload.get("audit_transaction_id")
        ):
            return _restore_audit_provenance(engine, state, current_payload)
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
        transaction_id=audit_transaction_id,
        fingerprint_context={"kind": "checkpoint_result", "checkpoint_id": state.id},
    )
    manager.append(txn.id, Operation(op="append", stream="checkpoints", record=record))
    result = manager.commit(txn.id)
    state.audit_transaction_id = txn.id
    state.audit_commit = result["commit"]
    state_store.save(engine, state)
    return state.audit_commit


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
    state_store.transition(engine, state, "DERIVATIONS_RUNNING")
    derivations_steps.run_derivations(engine, state)
    # Cleanup is deliberately performed after the first runtime receipt so the
    # user-input unlock ordering remains stable. Mark it pending now so the
    # first audit revision truthfully advertises the replaceable boundary.
    if state.worker_handle and "worker_cleanup" not in state.derivations:
        state.derivations["worker_cleanup"] = {"ok": False, "pending": True}
        state_store.save(engine, state)
    pending = any(not item.get("ok", False) for item in state.derivations.values())
    state.completed_at = utc_now()
    final_status = "COMMITTED_WITH_PENDING_DERIVATIONS" if pending else "DONE"
    state_store.transition(engine, state, final_status)
    # The audit record is written after derivations have observed the content
    # commit, but before the harness receipt is emitted. This makes the
    # runtime receipt and the durable receipt agree on audit_commit.
    write_checkpoint_record(engine, state)
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
        state_store.transition(engine, state, final_status)
    except Exception as exc:
        state.status = "COMMITTED_CONTEXT_PENDING"
        state_store.recover(engine, state, exc, preserve_status=True)
        raise
    # Worker cleanup is a replaceable post-commit housekeeping step. Keeping it
    # after unlock preserves the historical receipt/lock ordering while the
    # generated derivations above remain pinned to content_commit.
    derivations_steps.cleanup_worker(engine, state)
    cleanup_pending = any(not item.get("ok", False) for item in state.derivations.values())
    cleanup_status = "COMMITTED_WITH_PENDING_DERIVATIONS" if cleanup_pending else "DONE"
    if cleanup_status != final_status:
        state.completed_at = utc_now()
        state_store.transition(engine, state, cleanup_status)
        write_checkpoint_record(engine, state)
    current_receipt = receipt(engine, state)
    workspace.save_json(f"checkpoints/{state.id}/receipt.json", current_receipt)
    return {"ok": True, "checkpoint_id": state.id, "status": state.status, "receipt": current_receipt}
