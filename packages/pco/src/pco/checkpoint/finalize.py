from __future__ import annotations

import json
import uuid
from collections import Counter
from typing import Any

from mem_core.errors import MemError, ensure
from mem_core.models import Operation, utc_now
from mem_core.transaction import TransactionManager

from ..harness import receipt_payload_hash
from . import derivations as derivations_steps
from .errors import failed_attempt, structured_error, successful_attempt
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
    if hasattr(state, "consolidation_status"):
        state.consolidation_status = "committed"
    if hasattr(state, "consolidation_cursor_after"):
        state.consolidation_cursor_after = thread.last_consolidated_message_id
    _sync_pending_compaction(engine, state)
    state_store.transition(engine, state, "MEMORY_COMMITTED")
    return finalize_committed(engine, state)


def receipt(engine: Any, state: CheckpointState) -> dict[str, Any]:
    workspace = engine.workspace
    proposal_path = workspace.state_path(f"checkpoints/{state.id}/proposal.json")
    proposal = workspace.load_json(f"checkpoints/{state.id}/proposal.json") if proposal_path.is_file() else {"operations": []}
    derivations = _checkpoint_derivations(state)
    counts = Counter(
        operation.get("stream", "artifacts")
        for operation in proposal["operations"]
        if operation.get("stream") != "checkpoints"
    )
    meta_updated = counts.get("meta_revisions", 0) > 0
    approval = state.decision or ("not_required" if not state.protected_streams else None)
    promotion_hash = state.promotion_proposal_hash or state.proposal_hash
    promotion_streams = state.promotion_protected_streams or state.protected_streams
    intent = getattr(state, "intent", "compact")
    consolidation_status = getattr(state, "consolidation_status", None)
    if consolidation_status is None:
        consolidation_status = "committed"
    compaction_requested = bool(getattr(state, "compaction_requested", intent == "compact"))
    compaction_status = getattr(state, "compaction_status", None)
    if compaction_status is None:
        compaction_status = "completed" if state.compacted else ("pending" if compaction_requested else "not_requested")
    context_publication_status = getattr(state, "context_publication_status", None)
    if context_publication_status is None:
        context_publication_status = "completed" if state.context_published else "pending"
    summary = (
        f"记忆已更新，对话上下文 {'已压缩' if compaction_requested else '未压缩'}。"
        f"事件 {counts.get('events', 0)}，hypothesis {counts.get('hypotheses', 0)}；"
        f"Meta-memory {'已更新' if meta_updated else '未更新'}（{approval}）；Git {state.commit[:8] if state.commit else '复用/待提交'}"
    )
    return {
        "ok": True,
        "checkpoint_id": state.id,
        "trigger": state.trigger,
        "intent": intent,
        "status": state.status,
        "started_at": state.created_at,
        "completed_at": state.completed_at,
        "thread_id": state.thread_id,
        "harness_binding_id": state.harness_binding_id,
        "parent_session_id": state.parent_session_id,
        "worker": state.worker_handle,
        "archive_cursor": state.archive_cursor,
        "message_range": {"after": state.after_message_id, "through": state.through_message_id},
        "consolidation_cursor_before": getattr(state, "consolidation_cursor_before", None),
        "consolidation_cursor_after": getattr(state, "consolidation_cursor_after", None),
        "compaction_cursor_before": getattr(state, "compaction_cursor_before", None),
        "compaction_cursor_after": getattr(state, "compaction_cursor_after", None),
        "source_hashes": state.source_hashes,
        "consolidation_source_hashes": getattr(state, "consolidation_source_hashes", state.source_hashes),
        "operation_counts": dict(counts),
        "promotion_proposal": bool(promotion_streams),
        "protected_streams": promotion_streams,
        "approval_decision": approval,
        "authorization_id": state.decision_authorization_id,
        "authorization_provenance": {
            "question_request_id": state.decision_question_request_id,
        } if state.decision_question_request_id else None,
        "proposal_hash": promotion_hash,
        "final_proposal_hash": state.proposal_hash,
        "transaction_proposal_hash": state.transaction_proposal_hash,
        "transaction_fingerprint": state.transaction_fingerprint,
        "git_commit": state.commit,
        "content_commit": state.content_commit or state.commit,
        "consolidation": {
            "status": consolidation_status,
            "content_commit": state.content_commit or state.commit,
        },
        "context_publication": {
            "status": context_publication_status,
            "content_hash": (state.context_bundle or {}).get("content_hash") if isinstance(state.context_bundle, dict) else None,
        },
        "compaction": {
            "requested": compaction_requested,
            "status": compaction_status,
            "origin": getattr(state, "compaction_origin", None),
            "cursor_before": getattr(state, "compaction_cursor_before", None),
            "cursor_after": getattr(state, "compaction_cursor_after", None),
        },
        "receipt_generation": getattr(state, "receipt_generation", 0),
        "host_receipt_generation": getattr(state, "host_receipt_generation", None),
        "receipt_kind": getattr(state, "receipt_kind", "final"),
        "receipt_key": f"{state.id}:{getattr(state, 'receipt_generation', 0)}",
        "pending_acceptance": getattr(state, "pending_acceptance", "open"),
        "receipt_delivery": getattr(state, "receipt_outbox", None),
        "canonical_transaction": {
            "created": bool(
                getattr(state, "transaction_id", None)
                and consolidation_status != "no_op"
            ),
            "transaction_id": state.transaction_id if consolidation_status != "no_op" else None,
        },
        "thread_runtime": {
            "consolidation_cursor": engine.workspace.thread().last_consolidated_message_id,
            "compaction_cursor": engine.workspace.thread().compaction_cursor,
        },
        "derivation_source_commit": state.content_commit or state.commit,
        "audit_commit": state.audit_commit,
        "audit_transaction_id": state.audit_transaction_id,
        "meta_updated": meta_updated,
        "meta_revision": state.meta_revision,
        "continuation_updated": counts.get("continuations", 0) > 0,
        "continuation_revision": state.continuation_revision,
        "runtime": state.harness_runtime,
        "context_bundle": state.context_bundle,
        "versions": {
            "profile": f"{workspace.profile.name}@{workspace.profile.version}",
            "policy_hash": workspace.profile.policy_hash,
            "workflow": "consolidate@0.4.0",
            "skills": state.skill_versions,
        },
        "retry_count": state.retries,
        "error": state.error,
        "derivations": derivations,
        "summary": summary,
    }


def _json_compatible(value: Any) -> Any:
    """Convert derivation output to JSON without flattening structured errors."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
    return value


def _checkpoint_derivations(state: CheckpointState) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name, item in state.derivations.items():
        raw = item if isinstance(item, dict) else {"value": item}
        entry = _json_compatible(raw)
        if not isinstance(entry, dict):
            entry = {"value": entry}
        entry["ok"] = bool(raw.get("ok", False))
        if not entry["ok"]:
            entry.setdefault("pending", True)
        # Keep the normalized schema in runtime state too: receipt, state and
        # canonical checkpoint records must expose the same result fields,
        # structured error details, and recovery attempt history.
        state.derivations[name] = entry
        entry.setdefault("status", "completed" if entry["ok"] else "failed")
        result[name] = entry
    return result


def _checkpoint_payload(engine: Any, state: CheckpointState, *, audit_transaction_id: str | None = None) -> dict[str, Any]:
    """Build the immutable consolidation-only checkpoint projection.

    Compact, receipt delivery, and lock state live in ``CheckpointState`` and
    the runtime receipt.  They are intentionally not projected into the
    append-only memory repository, because those fields can change after the
    consolidation commit without changing canonical memory.
    """

    workspace = engine.workspace
    all_derivations = _checkpoint_derivations(state)
    derivations = {
        name: value
        for name, value in all_derivations.items()
        if name not in {"worker_cleanup", "context"}
    }
    pending = any(not item["ok"] for item in derivations.values())
    binding = workspace.binding()
    approval = state.decision or ("not_required" if not state.protected_streams else "no")
    return {
        "thread_id": binding.thread_id,
        "harness_binding_id": binding.id,
        "parent_session_id": state.parent_session_id,
        "archive_cursor": state.archive_cursor,
        "source_hashes": state.source_hashes,
        "consolidation_source_hashes": getattr(state, "consolidation_source_hashes", state.source_hashes),
        "worker": state.worker_handle,
        "runtime": state.harness_runtime,
        "trigger": state.trigger,
        "requested_intent": getattr(state, "intent", "compact"),
        "status": "committed_with_pending_derivations" if pending else "committed",
        "consolidation_status": getattr(state, "consolidation_status", "committed"),
        "archive_cursor": state.archive_cursor,
        "consolidation_cursor_before": getattr(state, "consolidation_cursor_before", state.after_message_id),
        "consolidation_cursor_after": getattr(state, "consolidation_cursor_after", state.through_message_id),
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
        "authorization_id": state.decision_authorization_id,
        "authorization_provenance": {
            "question_request_id": state.decision_question_request_id,
        } if state.decision_question_request_id else None,
        "protected_streams": state.protected_streams,
        "promotion_protected_streams": state.promotion_protected_streams,
        "meta_revision": state.meta_revision,
        "continuation_revision": state.continuation_revision,
        "derivations": derivations,
        "versions": {
            "profile": f"{workspace.profile.name}@{workspace.profile.version}",
            "policy_hash": workspace.profile.policy_hash,
            "workflow": "consolidate@0.4.0",
            "skills": state.skill_versions,
        },
        "warnings": [],
        "started_at": state.created_at,
        "ended_at": utc_now(),
        "retry_count": state.retries,
    }


def _raise_capability_error(value: Any, phase: str) -> None:
    error = structured_error(value, phase)
    details = {key: error[key] for key in ("stream", "record_id", "path", "value") if key in error}
    raise MemError(
        str(error["code"]),
        str(error["phase"]),
        str(error["message"]),
        retryable=bool(error.get("retryable", True)),
        recovery=list(error.get("recovery", [])),
        **details,
    )


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
    # Runtime-only no-op compactions must never enter the canonical memory
    # repository. A missing consolidation transaction is also a fail-closed
    # signal that this state has no canonical record to audit.
    if getattr(state, "consolidation_status", None) == "no_op" or not state.transaction_id:
        return None
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


def _compaction_requested(state: CheckpointState) -> bool:
    return bool(getattr(state, "compaction_requested", getattr(state, "intent", "compact") == "compact"))


def _sync_pending_compaction(engine: Any, state: CheckpointState) -> None:
    """Merge a pending Harness request written by a concurrent hook."""

    durable = state_store.load(engine)
    if durable.pending_compaction is None:
        return
    state.pending_compaction = durable.pending_compaction
    state.pending_compaction_request_ids = list(durable.pending_compaction_request_ids)
    state.compaction_requested = True
    if state.compaction_status == "not_requested":
        state.compaction_status = "pending"
    if state.compaction_origin is None:
        state.compaction_origin = durable.compaction_origin


def _advance_thread_compaction_cursor(engine: Any, state: CheckpointState) -> None:
    """Reconcile the thread cursor from a durably successful compact.

    The checkpoint state is persisted before this thread-runtime write. If the
    process stops between the two writes, retry can safely call this helper
    again without invoking native compact a second time.
    """

    target = getattr(state, "compaction_cursor_after", None)
    if not target:
        return
    thread = engine.workspace.thread()
    current = thread.compaction_cursor
    if current == target:
        return
    if current:
        message_ids = [record.get("id") for record in engine.workspace.repository.iter_records("messages")]
        try:
            current_index = message_ids.index(current)
            target_index = message_ids.index(target)
        except ValueError as exc:
            raise MemError(
                "COMPACTION_CURSOR_UNPROVEN",
                "native_compact",
                "Cannot prove the native compact cursor ordering",
                retryable=True,
            ) from exc
        ensure(
            target_index >= current_index,
            "COMPACTION_CURSOR_REGRESSION",
            "native_compact",
            "A compact retry cannot move the durable cursor backwards",
        )
    thread.compaction_cursor = target
    engine.workspace.save_thread(thread)


def _run_native_compact(engine: Any, state: CheckpointState) -> None:
    """Execute only the native-compaction phase, after derivations are durable."""

    _sync_pending_compaction(engine, state)
    # A previous process may have persisted compact success but stopped before
    # updating thread.json. Reconcile that durable success before deciding
    # whether the native side effect can be skipped.
    if getattr(state, "compaction_status", None) == "completed" and getattr(state, "compaction_cursor_after", None):
        _advance_thread_compaction_cursor(engine, state)
    if not _compaction_requested(state) or state.compacted:
        return
    if hasattr(state, "compaction_status"):
        state.compaction_status = "pending"
    state.failure_phase = "NATIVE_COMPACT"
    if hasattr(state, "native_compact_attempt_id") and not state.native_compact_attempt_id:
        supplied = getattr(engine.adapter, "native_compact_bypass", None)
        supplied_attempt = supplied.get("attempt_id") if isinstance(supplied, dict) else getattr(supplied, "attempt_id", None)
        state.native_compact_attempt_id = supplied_attempt or f"compact_{uuid.uuid4().hex}"
    state_store.save(engine, state)
    try:
        engine.adapter.compact()
    except Exception as exc:
        if hasattr(state, "compaction_status"):
            state.compaction_status = "failed"
        state.failure_phase = "NATIVE_COMPACT"
        state.error = {
            "code": "NATIVE_COMPACT_FAILED",
            "phase": "native_compact",
            "message": str(exc),
            "retryable": True,
            "recovery": ["Retry native compact from the persisted checkpoint boundary"],
        }
        state.status = "COMMITTED_CONTEXT_PENDING"
        state_store.save(engine, state)
        raise
    state.compacted = True
    state.failure_phase = None
    if hasattr(state, "compaction_status"):
        state.compaction_status = "completed"
    if hasattr(state, "compaction_cursor_after"):
        state.compaction_cursor_after = state.through_message_id
    state_store.transition(engine, state, "CONTEXT_COMPACTED")
    # The state transition above is the durable success marker. The thread
    # cursor is updated only after native compact has actually succeeded.
    _advance_thread_compaction_cursor(engine, state)


def _finish_receipt_and_unlock(engine: Any, state: CheckpointState, final_status: str) -> dict[str, Any]:
    """Publish one final receipt generation, then unlock exactly once.

    The operation lock closes the pending-acceptance window and makes the
    final snapshot/Host insert/unlock sequence linearizable across CLI and
    Harness-hook processes. A failed Host insert leaves the same generation
    pending and therefore retry is idempotent.
    """

    workspace = engine.workspace
    phase = "RECEIPT_INSERT"
    with state_store.operation_lock(engine):
        try:
            durable = state_store.load(engine)
            if durable.pending_compaction is not None:
                state.pending_compaction = durable.pending_compaction
                state.pending_compaction_request_ids = list(durable.pending_compaction_request_ids)
                state.compaction_requested = True
                if state.compaction_status == "not_requested":
                    state.compaction_status = "pending"
            # A request arriving before this point belongs to the compact tail;
            # do not publish a consolidate-only receipt. Native compact should
            # normally already have consumed it, but keep the lock fail-closed
            # if it did not.
            if state.pending_compaction is not None and state.compaction_status != "completed":
                state.failure_phase = "PENDING_COMPACTION"
                state.completed_at = None
                state.status = "COMMITTED_CONTEXT_PENDING"
                state_store.save(engine, state)
                current_receipt = receipt(engine, state)
                workspace.save_json(f"checkpoints/{state.id}/receipt.json", current_receipt)
                return {
                    "ok": True,
                    "checkpoint_id": state.id,
                    "status": state.status,
                    "receipt": current_receipt,
                    "checkpoint": state.model_dump(mode="json"),
                }

            # Close acceptance before building the final snapshot. Late
            # Harness requests will wait for this lock and create a new
            # compact checkpoint rather than mutating this outcome.
            state.pending_acceptance = "closed"
            if state.host_receipt_generation != state.receipt_generation:
                if state.receipt_generation <= (state.host_receipt_generation or 0):
                    state.receipt_generation = (state.host_receipt_generation or 0) + 1
                elif state.receipt_generation == 0:
                    state.receipt_generation = 1
                state.receipt_kind = "final"
                state.receipt_inserted = False
                state.receipt_outbox = None
            elif state.receipt_inserted and not state.input_unlocked and state.receipt_generation == 0:
                # Legacy state may say that generation 0 was inserted even
                # though a later pending compact reopened the operation. A
                # modern generation with a failed unlock must be retried
                # without re-inserting the already published Host receipt.
                state.receipt_generation += 1
                state.receipt_kind = "final"
                state.receipt_inserted = False
                state.receipt_outbox = None
            state.completed_at = utc_now()
            state.status = final_status
            state.failure_phase = phase
            state_store.save(engine, state)
            current_receipt = receipt(engine, state)
            payload_hash = receipt_payload_hash(current_receipt)
            existing_outbox = state.receipt_outbox
            if not (
                isinstance(existing_outbox, dict)
                and existing_outbox.get("receipt_key") == current_receipt["receipt_key"]
                and existing_outbox.get("payload_hash") == payload_hash
            ):
                existing_outbox = {
                    "receipt_key": current_receipt["receipt_key"],
                    "generation": state.receipt_generation,
                    "payload_hash": payload_hash,
                    "payload": {**current_receipt, "receipt_delivery": None},
                    "supersedes_key": (
                        f"{state.id}:{state.receipt_generation - 1}"
                        if state.receipt_generation > 1 else None
                    ),
                    "delivery_status": "pending",
                    "host_resource_id": None,
                    "host_disposition": None,
                    "created_at": utc_now(),
                    "acknowledged_at": None,
                    "attempts": 0,
                }
                state.receipt_outbox = existing_outbox
                state_store.save(engine, state)
            current_receipt = receipt(engine, state)
            workspace.save_json(f"checkpoints/{state.id}/receipt.json", current_receipt)

            if state.host_receipt_generation != state.receipt_generation:
                outbox = dict(state.receipt_outbox or {})
                outbox["attempts"] = int(outbox.get("attempts", 0)) + 1
                state.receipt_outbox = outbox
                state_store.save(engine, state)
                immutable_receipt = dict(outbox["payload"])
                acknowledgement = engine.adapter.publish_receipt(immutable_receipt, outbox)
                ensure(
                    isinstance(acknowledgement, dict)
                    and acknowledgement.get("key") == outbox["receipt_key"]
                    and int(acknowledgement.get("generation", -1)) == int(outbox["generation"])
                    and acknowledgement.get("payload_hash") == outbox["payload_hash"]
                    and isinstance(acknowledgement.get("host_resource_id"), str)
                    and acknowledgement.get("host_resource_id"),
                    "RECEIPT_ACK_INVALID",
                    "receipt",
                    "Host receipt acknowledgement did not prove key, generation, resource, and payload hash",
                    retryable=True,
                )
                state.receipt_outbox = {
                    **outbox,
                    "delivery_status": "acknowledged",
                    "host_resource_id": acknowledgement["host_resource_id"],
                    "host_disposition": acknowledgement.get("disposition"),
                    "acknowledged_at": utc_now(),
                }
                state.receipt_inserted = True
                state.host_receipt_generation = state.receipt_generation
                state_store.save(engine, state)

            # Unlock is strictly after durable confirmation that this exact
            # final generation reached the Host.
            if not state.input_unlocked:
                phase = "INPUT_UNLOCK"
                engine.adapter.unlock_input()
                state.input_unlocked = True
                state.failure_phase = None
                state_store.save(engine, state)
            # Persist the acknowledged outbox and unlock outcome together for
            # the runtime receipt. This is not a Host notification retry; it
            # makes the local snapshot reflect the already acknowledged
            # generation without touching the canonical record.
            workspace.save_json(f"checkpoints/{state.id}/receipt.json", receipt(engine, state))
        except Exception as exc:
            state.failure_phase = phase
            state_store.save(engine, state)
            raise

    # The pending marker can be retired only after the lock is released and
    # the final generation/unlock are durable.
    return {
        "ok": True,
        "checkpoint_id": state.id,
        "status": state.status,
        "receipt": receipt(engine, state),
        "checkpoint": state.model_dump(mode="json"),
    }


def finalize_after_derivations(engine: Any, state: CheckpointState) -> dict[str, Any]:
    """Finish cleanup, native compact, final receipt, and unlock in order."""

    _sync_pending_compaction(engine, state)
    if state.worker_handle and "worker_cleanup" not in state.derivations:
        state.derivations["worker_cleanup"] = {"ok": False, "pending": True, "status": "pending"}
        state_store.save(engine, state)
    # Cleanup is a prerequisite of the final snapshot. Its outcome must be
    # visible to both the canonical record and the Host receipt.
    if state.worker_handle and state.derivations.get("worker_cleanup", {}).get("pending"):
        derivations_steps.cleanup_worker(engine, state)
    _sync_pending_compaction(engine, state)
    pending = any(not item.get("ok", False) for item in state.derivations.values())
    final_status = "COMMITTED_WITH_PENDING_DERIVATIONS" if pending else "DONE"
    # The durable checkpoint result is recorded after derivation outcomes and
    # native compact is invoked. This record audits the canonical consolidation
    # only; compact-only retries never append another canonical record.
    current_record = engine.workspace.repository.current_records("checkpoints").get(state.id)
    if not (
        getattr(state, "consolidation_status", None) == "no_op"
        or (state.pending_compaction is not None and current_record is not None)
    ):
        write_checkpoint_record(engine, state)
    _run_native_compact(engine, state)
    return _finish_receipt_and_unlock(engine, state, final_status)


def finalize_committed(engine: Any, state: CheckpointState) -> dict[str, Any]:
    workspace = engine.workspace
    try:
        if state.context_bundle is None:
            rendered_context = workspace.profile.invoke(
                "context_renderer.render",
                repo_root=workspace.config.memory_root,
                output_path=workspace.config.state_root / "context" / "current.md",
                checkpoint_id=state.id,
                source_commit=state.content_commit,
            )
            if isinstance(rendered_context, dict) and rendered_context.get("ok") is False:
                _raise_capability_error(rendered_context.get("error") or rendered_context, "context")
            state.context_bundle = rendered_context
            workspace.save_json("context/current.json", state.context_bundle)
            state_store.save(engine, state)
        if not state.context_published:
            engine.adapter.publish_context(state.context_bundle)
            state.context_published = True
            if hasattr(state, "context_publication_status"):
                state.context_publication_status = "completed"
            state.derivations["context"] = {
                **successful_attempt(state.derivations.get("context", {}), state.context_bundle),
                "status": "completed",
            }
            state_store.transition(engine, state, "CONTEXT_PUBLISHED")
    except Exception as exc:
        state.status = "COMMITTED_CONTEXT_PENDING"
        state.derivations["context"] = failed_attempt(state.derivations.get("context", {}), exc, "context")
        state.derivations["context"]["status"] = "failed"
        state.error = state.derivations["context"]["error"]
        if hasattr(state, "context_publication_status"):
            state.context_publication_status = "failed"
        state_store.save(engine, state)
        raise

    # This is the only entry into replaceable derivations. Their errors are
    # persisted as pending/failed, but never prevent the native compact phase.
    state_store.transition(engine, state, "DERIVATIONS_RUNNING")
    derivations_steps.run_derivations(engine, state)
    return finalize_after_derivations(engine, state)


def finalize_noop_compact(engine: Any, state: CheckpointState) -> dict[str, Any]:
    """Run the compact side effect after reusing a successful checkpoint.

    No worker, transaction, canonical content commit, or derivation is
    created for this path.  The durable state and receipt still make the
    operation observable and retryable.
    """
    if state.context_bundle is None:
        context_path = engine.workspace.state_path("context/current.json")
        ensure(context_path.is_file(), "CONTEXT_BUNDLE_MISSING", "context", "No published context bundle is available to reuse")
        state.context_bundle = engine.workspace.load_json("context/current.json")
    state.context_published = True
    if hasattr(state, "context_publication_status"):
        state.context_publication_status = "completed"
    if hasattr(state, "consolidation_status") and state.transaction_id is None:
        state.consolidation_status = "no_op"
    state_store.save(engine, state)
    return finalize_after_derivations(engine, state)
