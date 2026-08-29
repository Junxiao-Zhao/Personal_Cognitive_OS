from __future__ import annotations

import difflib
import copy
import json
import math
import uuid
from collections import Counter
from typing import Any

from mem_core.errors import ensure
from mem_core.models import Operation, proposal_hash

from ..harness import WORKER_RESULT_SCHEMA, WorkerResult, normalize_external_url, receipt_result_urls
from . import finalize as finalize_steps
from . import state as state_store
from .state import CheckpointState


def worker_profile_contract(engine: Any) -> dict[str, Any]:
    profile = engine.workspace.profile
    system_managed = {"messages", "sources", "checkpoints", "search_receipts"}
    streams: dict[str, Any] = {}
    for name, stream in profile.config.streams.items():
        if name in system_managed or stream.write_policy == "read_only":
            continue
        streams[name] = {
            "write_policy": stream.write_policy,
            "schema_version": stream.schema_version,
            "record_schema": json.loads((profile.root / stream.schema_path).read_text(encoding="utf-8")),
        }
    prompt_path = profile.root / "prompts" / "consolidate.md"
    return {
        "profile": f"{profile.name}@{profile.version}",
        "policy_hash": profile.policy_hash,
        "operation_contract": WORKER_RESULT_SCHEMA,
        "source_materialization": {
            "owner": "wrapper",
            "reader_contract": ["locator", "reader", "normalized_content", "media_type", "read_metadata"],
            "snapshot_write_permission": False,
        },
        "allowed_streams": streams,
        "required_invariants": [
            "Every append operation includes an allowed stream and a record matching that stream's complete JSON Schema.",
            "Produce exactly one continuations append operation for every successful consolidate or rejection revision.",
            "Do not output messages, sources, or checkpoints operations; the wrapper owns those streams.",
            "Do not output source snapshot write_artifact operations; source materialization and snapshots belong to the wrapper.",
            "Assistant messages are context only and cannot be used as user evidence.",
            "Only meta_revisions is protected and it is a proposal until wrapper approval.",
        ],
        "consolidate_prompt": prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else "",
    }


def freeze(engine: Any, state: CheckpointState) -> dict[str, Any]:
    workspace = engine.workspace
    thread = workspace.thread()
    messages = list(workspace.repository.iter_records("messages"))
    selected: list[dict[str, Any]] = []
    started = thread.last_consolidated_message_id is None
    for message in messages:
        if started:
            selected.append(message)
        elif message["id"] == thread.last_consolidated_message_id:
            started = True
    source_result = engine.sources.collect_diffs()
    # A source-only update is a valid consolidate boundary even when the
    # conversation cursor did not move.  A truly empty boundary is rejected by
    # the request layer as a no-op and never reaches the worker.
    if selected:
        ensure(selected[-1]["id"] == state.through_message_id, "CHECKPOINT_BOUNDARY_CHANGED", "freeze", "Frozen message boundary no longer matches the archive")
    else:
        ensure(source_result["changes"], "CHECKPOINT_RANGE_EMPTY", "freeze", "There are no unconsolidated public messages or changed sources")
    current = {
        "meta": workspace.repository.current_records("meta_revisions").get("meta_current"),
        "continuation": workspace.repository.current_records("continuations").get("continuation_current"),
        "events": list(workspace.repository.current_records("events").values()),
        "hypotheses": list(workspace.repository.current_records("hypotheses").values()),
    }
    frozen = {
        "checkpoint_id": state.id,
        "trigger": state.trigger,
        "message_range": {"after": state.after_message_id, "through": state.through_message_id},
        "messages": selected,
        "source_changes": source_result["changes"],
        "source_operations": [operation.normalized() for operation in source_result["operations"]],
        "source_hashes": source_result["source_hashes"],
        "canonical_context": current,
        "base_commit": workspace.repository.head(),
        "profile": f"{workspace.profile.name}@{workspace.profile.version}",
        "policy_hash": workspace.profile.policy_hash,
        "profile_contract": worker_profile_contract(engine),
    }
    workspace.save_json(f"checkpoints/{state.id}/frozen.json", frozen)
    return frozen


def validate_rejection_candidate(engine: Any, state: CheckpointState, result: WorkerResult) -> None:
    if state.decision != "no":
        return
    ensure(
        not any(op.stream == "meta_revisions" for op in result.operations),
        "REJECTION_REPROPOSED_META",
        "worker_validation",
        "A rejection revision cannot propose Meta-memory again in the same checkpoint",
    )
    initial = engine.workspace.load_json(f"checkpoints/{state.id}/proposal-initial.json")
    promoted_hypotheses = {
        hypothesis_id
        for operation in initial.get("operations", [])
        if operation.get("op") == "append" and operation.get("stream") == "meta_revisions"
        for hypothesis_id in operation.get("record", {}).get("payload", {}).get("promotion_refs", [])
    }
    if not promoted_hypotheses:
        promoted_hypotheses = {
            operation.get("record", {}).get("id")
            for operation in initial.get("operations", [])
            if operation.get("op") == "append" and operation.get("stream") == "hypotheses"
        }
        promoted_hypotheses.discard(None)
    decision_ref = f"message:{state.decision_message_id}"
    rejection_revisions = {
        operation.record["id"]: operation.record
        for operation in result.operations
        if operation.op == "append"
        and operation.stream == "hypotheses"
        and operation.record is not None
        and operation.record.get("payload", {}).get("status") in {"disputed", "rejected"}
        and decision_ref in operation.record.get("payload", {}).get("counter_evidence_refs", [])
        and str(operation.record.get("payload", {}).get("revision_reason", "")).strip()
    }
    ensure(
        promoted_hypotheses and promoted_hypotheses.issubset(rejection_revisions),
        "REJECTION_HYPOTHESIS_REVISION_REQUIRED",
        "worker_validation",
        "A rejection must append disputed/rejected revisions for the promoted hypotheses and cite the archived decision",
        value={"required": sorted(promoted_hypotheses), "received": sorted(rejection_revisions), "decision_ref": decision_ref},
    )


def effective_search_receipts(engine: Any, state: CheckpointState, result: WorkerResult) -> list[dict[str, Any]]:
    workspace = engine.workspace
    receipts: list[dict[str, Any]] = []
    if state.decision == "no":
        initial_path = workspace.state_path(f"checkpoints/{state.id}/worker-result-initial.json")
        if initial_path.exists():
            receipts.extend(workspace.load_json(f"checkpoints/{state.id}/worker-result-initial.json").get("search_receipts", []))
    receipts.extend(result.search_receipts)
    by_id: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        receipt_id = str(receipt.get("id", ""))
        ensure(receipt_id, "SEARCH_RECEIPT_INVALID", "worker_validation", "Captured search receipt is missing an id")
        ensure(
            receipt_id not in by_id or by_id[receipt_id] == receipt,
            "SEARCH_RECEIPT_CONFLICT",
            "worker_validation",
            "Captured search receipt id was reused with different content",
            record_id=receipt_id,
        )
        # Keep historical v1 receipts as v1.  This only enriches a receipt
        # arriving through the current worker-result boundary so the existing
        # v1 fixture remains readable without changing the schema identity of
        # old canonical receipts.
        receipt_result_urls(receipt)
        by_id[receipt_id] = receipt
    return list(by_id.values())


def validate_continuation(engine: Any, state: CheckpointState, operations: list[Operation]) -> None:
    continuation = [op.record for op in operations if op.op == "append" and op.stream == "continuations"]
    ensure(
        len(continuation) == 1,
        "CONTINUATION_REQUIRED",
        "worker_validation",
        "A checkpoint must append exactly one continuation revision",
    )
    continuation_limit = int(engine.workspace.profile.raw.get("continuation", {}).get("max_tokens", 1200))
    continuation_text = json.dumps(
        continuation[0]["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    estimated_tokens = math.ceil(sum(1.0 if ord(char) > 127 else 0.25 for char in continuation_text))
    ensure(
        estimated_tokens <= continuation_limit,
        "CONTINUATION_TOO_LONG",
        "worker_validation",
        f"Continuation exceeds Profile limit of {continuation_limit} tokens",
        value={"estimated_tokens": estimated_tokens, "max_tokens": continuation_limit},
    )


def canonical_fingerprint_context(state: CheckpointState, frozen: dict[str, Any]) -> dict[str, Any]:
    """Build the stable cognitive transaction fingerprint boundary.

    Request provenance, checkpoint identity, retries, timestamps and audit
    receipts belong to the operation/audit layer and must not fork canonical
    cognitive content.
    """

    return {
        "message_range": {
            "after": state.after_message_id,
            "through": state.through_message_id,
        },
        "source_hashes": frozen.get("source_hashes", {}),
        "worker_contract": frozen.get("profile_contract", {}),
        "profile": frozen["profile"],
        "policy_hash": frozen["policy_hash"],
    }


def _main_evidence(engine: Any, refs: list[str]) -> list[dict[str, Any]]:
    """Resolve the user-facing evidence behind protected Meta references."""

    evidence: list[dict[str, Any]] = []
    stream_aliases = {"message": "messages"}
    for reference in refs:
        prefix, separator, record_id = reference.partition(":")
        if not separator or not record_id:
            continue
        stream = stream_aliases.get(prefix, prefix)
        try:
            record = engine.workspace.repository.current_records(stream).get(record_id)
        except Exception:
            record = None
        if not record:
            continue
        payload = record.get("payload", {})
        item: dict[str, Any] = {
            "ref": reference,
            "stream": stream,
            "record_id": record.get("id"),
        }
        if stream == "messages":
            item.update(
                {
                    "role": payload.get("role"),
                    "content": payload.get("content", ""),
                    "created_at": payload.get("created_at"),
                    "native_message_id": payload.get("native_message_id"),
                }
            )
        else:
            item["payload"] = payload
        evidence.append(item)
    return evidence


def protected_diff(engine: Any, operations: list[Operation]) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    current = engine.workspace.repository.current_records("meta_revisions").get("meta_current")
    before = json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True).splitlines(keepends=True) if current else []
    for operation in operations:
        if operation.op != "append" or operation.stream != "meta_revisions" or operation.record is None:
            continue
        after = json.dumps(operation.record, ensure_ascii=False, indent=2, sort_keys=True).splitlines(keepends=True)
        diff = "".join(
            difflib.unified_diff(
                before,
                after,
                fromfile="meta_current@current",
                tofile=f"meta_current@{operation.record['revision']}",
            )
        )
        diffs.append(
            {
                "stream": "meta_revisions",
                "record_id": operation.record["id"],
                "revision": operation.record["revision"],
                "diff": diff,
                "evidence_refs": operation.record["payload"].get("evidence_refs", []),
                "main_evidence": _main_evidence(
                    engine,
                    operation.record["payload"].get("evidence_refs", []),
                ),
            }
        )
    return diffs


def prepare_candidate(engine: Any, state: CheckpointState, frozen: dict[str, Any], result: WorkerResult) -> dict[str, Any]:
    workspace = engine.workspace
    # A Harness overflow request may have been merged while the worker was
    # running. Import it before any proposal/commit transition can overwrite
    # the durable state with this older in-memory object.
    finalize_steps._sync_pending_compaction(engine, state)
    validate_rejection_candidate(engine, state, result)
    search_receipts = effective_search_receipts(engine, state, result)
    persisted_result = {
        "operations": [operation.normalized() for operation in result.operations],
        "search_receipts": search_receipts,
        "diagnostics": result.diagnostics,
        "skill_versions": result.skill_versions,
        "runtime_info": result.runtime_info,
    }
    initial_result_path = workspace.state_path(f"checkpoints/{state.id}/worker-result-initial.json")
    if not initial_result_path.exists():
        workspace.save_json(f"checkpoints/{state.id}/worker-result-initial.json", persisted_result)
    if state.decision == "no":
        workspace.save_json(f"checkpoints/{state.id}/worker-result-revised.json", persisted_result)
    allowed_worker_streams = set(frozen.get("profile_contract", {}).get("allowed_streams", {}))
    for operation in result.operations:
        ensure(
            operation.op == "append" and operation.stream in allowed_worker_streams,
            "WORKER_OPERATION_NOT_ALLOWED",
            "worker_validation",
            "Worker returned an operation owned by the wrapper or outside its Profile allowlist",
            stream=operation.stream,
            value=operation.normalized(),
            recovery=["Retry the same frozen boundary with only profile_contract.allowed_streams append operations"],
        )
    receipt_operations = [
        Operation(op="append", stream="search_receipts", record=receipt)
        for receipt in search_receipts
    ]
    for operation in result.operations:
        if operation.op != "append" or operation.stream not in {"psychologies", "philosophies"} or operation.record is None:
            continue
        for external_ref in operation.record.get("payload", {}).get("external_refs", []):
            url = normalize_external_url(external_ref.get("url", ""))
            matches = [
                receipt
                for receipt in search_receipts
                if url and url in receipt_result_urls(receipt)
            ]
            if matches:
                # The wrapper, not worker-authored free text, binds the
                # canonical concept to a completed captured tool call.
                external_ref["search_receipt"] = matches[-1]["id"]
    source_operations = [Operation.model_validate(item) for item in frozen.get("source_operations", [])]
    operations = [*source_operations, *receipt_operations, *result.operations]
    validate_continuation(engine, state, operations)
    state.harness_runtime.update(result.runtime_info)
    state.skill_versions = dict(result.skill_versions)
    state.operation_counts = dict(Counter(operation.stream or "artifacts" for operation in operations))
    proposed_meta = [
        operation.record
        for operation in operations
        if operation.op == "append" and operation.stream == "meta_revisions" and operation.record
    ]
    proposed_continuation = [
        operation.record
        for operation in operations
        if operation.op == "append" and operation.stream == "continuations" and operation.record
    ]
    state.meta_revision = proposed_meta[-1]["revision"] if proposed_meta else None
    state.continuation_revision = proposed_continuation[-1]["revision"] if proposed_continuation else None
    protected_operations = [
        operation
        for operation in operations
        if operation.op == "append"
        and operation.stream is not None
        and workspace.profile.stream(operation.stream).write_policy == "user_approval"
    ]
    approval_basis = []
    for operation in protected_operations or operations:
        normalized = operation.normalized()
        if normalized.get("stream") == "meta_revisions" and isinstance(normalized.get("record"), dict):
            normalized = copy.deepcopy(normalized)
            normalized["record"].get("payload", {}).pop("approval_ref", None)
        approval_basis.append(normalized)
    approval_receipt_id = f"approval_{proposal_hash([Operation.model_validate(item) for item in approval_basis])}"
    state.approval_receipt_id = approval_receipt_id
    for operation in operations:
        if operation.op == "append" and operation.stream == "meta_revisions" and operation.record is not None:
            operation.record["payload"]["approval_ref"] = approval_receipt_id
    reviewed_proposal_hash = proposal_hash(protected_operations or operations)
    if protected_operations and state.promotion_proposal_hash is None:
        state.promotion_proposal_hash = reviewed_proposal_hash
        state.promotion_protected_streams = sorted(
            {operation.stream for operation in protected_operations if operation.stream}
        )
    transaction_id = f"txn_{state.id}_{uuid.uuid4().hex[:8]}"
    fingerprint_context = canonical_fingerprint_context(state, frozen)
    # Keep the Host-reviewed protected-operation hash distinct from the full
    # transaction proposal hash. Approval attachment must bind to the former;
    # the latter also includes wrapper-owned auto operations such as receipts.
    fingerprint_context["reviewed_proposal_hash"] = reviewed_proposal_hash
    transaction = engine.manager.begin(
        transaction_id=transaction_id,
        fingerprint_context=fingerprint_context,
    )
    for operation in operations:
        engine.manager.append(transaction.id, operation)
    validation = engine.manager.validate(transaction.id)
    txn_state = engine.manager.load(transaction.id)
    state.transaction_id = transaction.id
    state.transaction_fingerprint = txn_state.transaction_fingerprint
    state.proposal_hash = reviewed_proposal_hash
    state.transaction_proposal_hash = txn_state.proposal_hash
    if protected_operations and state.approval_challenge_id is None:
        state.approval_challenge_id = f"challenge_{uuid.uuid4().hex}"
    state.protected_streams = validation["protected_streams"]
    protected_diffs = protected_diff(engine, operations)
    proposal = {
        "checkpoint_id": state.id,
        "transaction_id": transaction.id,
        "worker_handle": state.worker_handle,
        "profile": frozen["profile"],
        "message_range": {"after": state.after_message_id, "through": state.through_message_id},
        "base_commit": txn_state.base_commit,
        "operations": [operation.normalized() for operation in operations],
        "skill_versions": result.skill_versions,
        "policy_hash": frozen["policy_hash"],
        "protected_streams": state.protected_streams,
        "proposal_hash": state.proposal_hash,
        "transaction_proposal_hash": state.transaction_proposal_hash,
        "approval_challenge_id": state.approval_challenge_id,
        "transaction_fingerprint": state.transaction_fingerprint,
        "diagnostics": result.diagnostics,
        "protected_diff": protected_diffs,
        "main_evidence": [
            evidence
            for diff in protected_diffs
            for evidence in diff.get("main_evidence", [])
        ],
    }
    initial_path = workspace.state_path(f"checkpoints/{state.id}/proposal-initial.json")
    if not initial_path.exists():
        workspace.save_json(f"checkpoints/{state.id}/proposal-initial.json", proposal)
    if state.decision == "no":
        workspace.save_json(f"checkpoints/{state.id}/proposal-revised.json", proposal)
    elif state.decision == "yes":
        workspace.save_json(f"checkpoints/{state.id}/proposal-approved.json", proposal)
    workspace.save_json(f"checkpoints/{state.id}/proposal.json", proposal)
    state_store.transition(engine, state, "PROPOSAL_VALIDATED")
    if state.protected_streams:
        state_store.transition(engine, state, "AWAITING_META_APPROVAL")
        return {"ok": True, "checkpoint_id": state.id, "status": state.status, "approval_required": True, "proposal": proposal}
    state_store.transition(engine, state, "FINAL_CHANGESET_VALIDATED")
    result = finalize_steps.commit_and_finalize(engine, state)
    if state_store.load(engine).pending_compaction is not None:
        # Local import avoids the steps <-> recovery module cycle. The
        # original consolidate state remains authoritative; recovery only
        # appends the compact-only tail after publication.
        from . import recovery as recovery_steps

        return recovery_steps.resume_pending_compaction(engine, result)
    return result
