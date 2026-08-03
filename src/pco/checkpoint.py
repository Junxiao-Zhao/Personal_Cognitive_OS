from __future__ import annotations

import json
import uuid
import difflib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from mem_core.errors import MemError, ensure
from mem_core.models import Operation, proposal_hash, utc_now
from mem_core.transaction import TransactionManager
from pydantic import BaseModel, ConfigDict, Field

from .archive import ConversationArchive
from .backlinks import build as build_backlinks
from .context import render as render_context
from .harness import WORKER_RESULT_SCHEMA, HarnessAdapter, WorkerHandle, WorkerResult
from .projections import project_affine, project_markdown
from .retrieval import build_index
from .sources import SourceManager
from .workspace import Workspace


CheckpointStatus = Literal[
    "CHECKPOINT_REQUESTED",
    "INPUT_LOCKED",
    "TRANSCRIPT_FROZEN",
    "WORKER_RUNNING",
    "PROPOSAL_VALIDATED",
    "AWAITING_META_APPROVAL",
    "FINAL_CHANGESET_VALIDATED",
    "MEMORY_COMMITTED",
    "COMMITTED_CONTEXT_PENDING",
    "CONTEXT_PUBLISHED",
    "CONTEXT_COMPACTED",
    "RECEIPT_INSERTED",
    "DERIVATIONS_RUNNING",
    "DONE",
    "COMMITTED_WITH_PENDING_DERIVATIONS",
    "RECOVERY",
    "ABORTED",
]


class CheckpointState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    trigger: Literal["manual", "auto"]
    status: CheckpointStatus
    after_message_id: str | None
    through_message_id: str
    thread_id: str
    harness_binding_id: str
    parent_session_id: str
    archive_cursor: str | None = None
    source_hashes: dict[str, str] = Field(default_factory=dict)
    harness_runtime: dict[str, Any] = Field(default_factory=dict)
    operation_counts: dict[str, int] = Field(default_factory=dict)
    skill_versions: dict[str, str] = Field(default_factory=dict)
    meta_revision: int | None = None
    continuation_revision: int | None = None
    transaction_id: str | None = None
    transaction_fingerprint: str | None = None
    proposal_hash: str | None = None
    transaction_proposal_hash: str | None = None
    protected_streams: list[str] = Field(default_factory=list)
    promotion_proposal_hash: str | None = None
    promotion_protected_streams: list[str] = Field(default_factory=list)
    worker_handle: dict[str, str] | None = None
    decision: Literal["yes", "no"] | None = None
    decision_message_id: str | None = None
    commit: str | None = None
    context_bundle: dict[str, Any] | None = None
    context_published: bool = False
    compacted: bool = False
    receipt_inserted: bool = False
    retries: int = 0
    failure_phase: str | None = None
    error: dict[str, Any] | None = None
    derivations: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    completed_at: str | None = None


class CheckpointEngine:
    """Durable manual/auto checkpoint state machine.

    Both triggers enter ``request``; only the trigger field differs. All
    durable boundaries are persisted before the next external side effect.
    """

    def __init__(self, workspace: Workspace, adapter: HarnessAdapter) -> None:
        self.workspace = workspace
        self.adapter = adapter
        self.archive = ConversationArchive(workspace)
        self.sources = SourceManager(workspace)
        self.manager = TransactionManager(workspace.repository, workspace.config.state_root)

    @property
    def active_path(self) -> Path:
        return self.workspace.config.state_root / "active-checkpoint.json"

    def _checkpoint_dir(self, checkpoint_id: str) -> Path:
        return self.workspace.config.state_root / "checkpoints" / checkpoint_id

    def _save(self, state: CheckpointState) -> None:
        state.updated_at = utc_now()
        self.workspace.save_json("active-checkpoint.json", state.model_dump(mode="json"))
        self.workspace.save_json(
            f"checkpoints/{state.id}/state.json", state.model_dump(mode="json")
        )
        if state.status not in {"DONE", "COMMITTED_WITH_PENDING_DERIVATIONS", "ABORTED"}:
            self.adapter.lock_input(state.id, state.status)

    def _load(self) -> CheckpointState:
        ensure(self.active_path.is_file(), "CHECKPOINT_NOT_FOUND", "checkpoint", "There is no active checkpoint")
        return CheckpointState.model_validate(self.workspace.load_json("active-checkpoint.json"))

    def status(self) -> dict[str, Any]:
        if not self.active_path.exists():
            return {"ok": True, "active": False}
        state = self._load()
        return {"ok": True, "active": state.status not in {"DONE", "COMMITTED_WITH_PENDING_DERIVATIONS", "ABORTED"}, "checkpoint": state.model_dump(mode="json")}

    def should_auto_checkpoint(self) -> bool:
        if self.active_path.exists():
            state = self._load()
            if state.status not in {"DONE", "COMMITTED_WITH_PENDING_DERIVATIONS", "ABORTED"}:
                return False
        return self.adapter.estimate_context_usage() >= self.workspace.config.checkpoint.trigger_ratio

    def _worker_profile_contract(self) -> dict[str, Any]:
        profile = self.workspace.profile
        system_managed = {"messages", "sources", "checkpoints"}
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
            "allowed_streams": streams,
            "required_invariants": [
                "Every append operation includes an allowed stream and a record matching that stream's complete JSON Schema.",
                "Produce exactly one continuations append operation for every successful consolidate or rejection revision.",
                "Do not output messages, sources, or checkpoints operations; the wrapper owns those streams.",
                "Assistant messages are context only and cannot be used as user evidence.",
                "Only meta_revisions is protected and it is a proposal until wrapper approval.",
            ],
            "consolidate_prompt": prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else "",
        }

    def _freeze(self, state: CheckpointState) -> dict[str, Any]:
        thread = self.workspace.thread()
        messages = list(self.workspace.repository.iter_records("messages"))
        selected: list[dict[str, Any]] = []
        started = thread.last_consolidated_message_id is None
        for message in messages:
            if started:
                selected.append(message)
            elif message["id"] == thread.last_consolidated_message_id:
                started = True
        if selected and selected[0]["id"] == thread.last_consolidated_message_id:
            selected = selected[1:]
        selected = [message for message in selected if message["id"] != thread.last_consolidated_message_id]
        ensure(selected, "CHECKPOINT_RANGE_EMPTY", "freeze", "There are no unconsolidated public messages")
        ensure(selected[-1]["id"] == state.through_message_id, "CHECKPOINT_BOUNDARY_CHANGED", "freeze", "Frozen message boundary no longer matches the archive")
        source_result = self.sources.collect_diffs()
        current = {
            "meta": self.workspace.repository.current_records("meta_revisions").get("meta_current"),
            "continuation": self.workspace.repository.current_records("continuations").get("continuation_current"),
            "events": list(self.workspace.repository.current_records("events").values()),
            "hypotheses": list(self.workspace.repository.current_records("hypotheses").values()),
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
            "base_commit": self.workspace.repository.head(),
            "profile": f"{self.workspace.profile.name}@{self.workspace.profile.version}",
            "policy_hash": self.workspace.profile.policy_hash,
            "profile_contract": self._worker_profile_contract(),
        }
        self.workspace.save_json(f"checkpoints/{state.id}/frozen.json", frozen)
        return frozen

    def request(self, trigger: Literal["manual", "auto"]) -> dict[str, Any]:
        if trigger == "auto":
            ensure(self.should_auto_checkpoint(), "AUTO_THRESHOLD_NOT_REACHED", "checkpoint", "Automatic checkpoint threshold has not been reached")
        if self.active_path.exists():
            previous = self._load()
            ensure(previous.status in {"DONE", "COMMITTED_WITH_PENDING_DERIVATIONS", "ABORTED"}, "CHECKPOINT_ACTIVE", "checkpoint", f"Checkpoint {previous.id} is still {previous.status}")
        session_id = self.adapter.attach_or_create()
        binding = self.workspace.binding()
        if binding.native_session_id is None:
            binding.native_session_id = session_id
            self.workspace.save_binding(binding)
        ensure(binding.native_session_id == session_id, "HARNESS_BINDING_CONFLICT", "checkpoint", "Adapter session does not match the active PCO binding")
        thread = self.workspace.thread()
        new_messages = self.adapter.archive_messages_since(thread.archive_cursor)
        self.archive.archive(new_messages)
        thread = self.workspace.thread()
        ensure(thread.last_archived_message_id is not None, "CHECKPOINT_RANGE_EMPTY", "checkpoint", "No archived conversation is available")
        state = CheckpointState(
            id=f"ckpt_{uuid.uuid4().hex}",
            trigger=trigger,
            status="CHECKPOINT_REQUESTED",
            after_message_id=thread.last_consolidated_message_id,
            through_message_id=thread.last_archived_message_id,
            thread_id=thread.thread_id,
            harness_binding_id=binding.id,
            parent_session_id=session_id,
            archive_cursor=thread.archive_cursor,
            harness_runtime=self.adapter.runtime_info(),
        )
        self._save(state)
        try:
            state.status = "INPUT_LOCKED"
            self._save(state)
            frozen = self._freeze(state)
            state.source_hashes = dict(frozen.get("source_hashes", {}))
            state.status = "TRANSCRIPT_FROZEN"
            self._save(state)
            handle = self.adapter.spawn_worker(
                {"checkpoint_id": state.id, "worker_id": f"worker_{uuid.uuid4().hex}", "frozen_input_path": str(self._checkpoint_dir(state.id) / "frozen.json")}
            )
            state.worker_handle = handle.as_dict()
            state.status = "WORKER_RUNNING"
            self._save(state)
            result = self.adapter.resume_worker(handle, {"kind": "consolidate", "frozen": frozen})
            return self._prepare_candidate(state, frozen, result)
        except Exception as exc:
            self._recover(state, exc)
            raise

    def _checkpoint_operation(
        self,
        state: CheckpointState,
        transaction_id: str,
        operations: list[Operation],
        result: WorkerResult,
        reviewed_proposal_hash: str,
        protected_operations: list[Operation],
    ) -> Operation:
        continuation = [op.record for op in operations if op.op == "append" and op.stream == "continuations"]
        ensure(len(continuation) == 1, "CONTINUATION_REQUIRED", "worker_validation", "A checkpoint must append exactly one continuation revision")
        meta = [op.record for op in operations if op.op == "append" and op.stream == "meta_revisions"]
        counts = Counter(op.stream or "artifacts" for op in operations)
        binding = self.workspace.binding()
        record = {
            "id": state.id,
            "revision": 1,
            "recorded_at": utc_now(),
            "schema_version": "pco/checkpoint/v1",
            "payload": {
                "thread_id": binding.thread_id,
                "harness_binding_id": binding.id,
                "parent_session_id": state.parent_session_id,
                "archive_cursor": state.archive_cursor,
                "source_hashes": state.source_hashes,
                "worker": state.worker_handle,
                "runtime": state.harness_runtime,
                "trigger": state.trigger,
                "status": "committed",
                "message_range": {"after": state.after_message_id, "through": state.through_message_id},
                "transaction_id": transaction_id,
                "git_commit": None,
                "operation_counts": dict(counts),
                "proposal_hash": reviewed_proposal_hash,
                "promotion_proposal_hash": state.promotion_proposal_hash,
                "approval_decision": "yes" if meta else (state.decision or "not_required"),
                "protected_streams": sorted(
                    {operation.stream for operation in protected_operations if operation.stream}
                ),
                "promotion_protected_streams": state.promotion_protected_streams,
                "meta_revision": meta[-1]["revision"] if meta else None,
                "continuation_revision": continuation[0]["revision"],
                "derivations": {"index": "scheduled", "backlinks": "scheduled", "projection": "scheduled"},
                "versions": {"profile": f"pco@{self.workspace.profile.version}", "policy_hash": self.workspace.profile.policy_hash, "workflow": "consolidate@0.3.1", "skills": result.skill_versions},
                "warnings": [],
                "started_at": state.created_at,
                "ended_at": utc_now(),
                "retry_count": state.retries,
            },
        }
        return Operation(op="append", stream="checkpoints", record=record)

    def _prepare_candidate(self, state: CheckpointState, frozen: dict[str, Any], result: WorkerResult) -> dict[str, Any]:
        persisted_result = {
            "operations": [operation.normalized() for operation in result.operations],
            "diagnostics": result.diagnostics,
            "skill_versions": result.skill_versions,
            "runtime_info": result.runtime_info,
        }
        initial_result_path = self.workspace.state_path(f"checkpoints/{state.id}/worker-result-initial.json")
        if not initial_result_path.exists():
            self.workspace.save_json(f"checkpoints/{state.id}/worker-result-initial.json", persisted_result)
        if state.decision == "no":
            self.workspace.save_json(f"checkpoints/{state.id}/worker-result-revised.json", persisted_result)
        source_operations = [Operation.model_validate(item) for item in frozen.get("source_operations", [])]
        operations = [*source_operations, *result.operations]
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
            and self.workspace.profile.stream(operation.stream).write_policy == "user_approval"
        ]
        approval_receipt_id = f"approval_{state.id}"
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
        checkpoint_operation = self._checkpoint_operation(
            state,
            transaction_id,
            operations,
            result,
            reviewed_proposal_hash,
            protected_operations,
        )
        operations.append(checkpoint_operation)
        transaction = self.manager.begin(
            transaction_id=transaction_id,
            fingerprint_context={
                "thread_id": self.workspace.thread().thread_id,
                "harness_binding_id": self.workspace.binding().id,
                "message_range": {"after": state.after_message_id, "through": state.through_message_id},
                "source_hashes": frozen.get("source_hashes", {}),
                "profile": frozen["profile"],
                "policy_hash": frozen["policy_hash"],
                "checkpoint_id": state.id,
                "decision_message_id": state.decision_message_id,
                "reviewed_proposal_hash": reviewed_proposal_hash,
            },
        )
        for operation in operations:
            self.manager.append(transaction.id, operation)
        validation = self.manager.validate(transaction.id)
        txn_state = self.manager.load(transaction.id)
        state.transaction_id = transaction.id
        state.transaction_fingerprint = txn_state.transaction_fingerprint
        state.proposal_hash = reviewed_proposal_hash
        state.transaction_proposal_hash = txn_state.proposal_hash
        state.protected_streams = validation["protected_streams"]
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
            "transaction_fingerprint": state.transaction_fingerprint,
            "diagnostics": result.diagnostics,
            "protected_diff": self._protected_diff(operations),
        }
        initial_path = self.workspace.state_path(f"checkpoints/{state.id}/proposal-initial.json")
        if not initial_path.exists():
            self.workspace.save_json(f"checkpoints/{state.id}/proposal-initial.json", proposal)
        if state.decision == "no":
            self.workspace.save_json(f"checkpoints/{state.id}/proposal-revised.json", proposal)
        elif state.decision == "yes":
            self.workspace.save_json(f"checkpoints/{state.id}/proposal-approved.json", proposal)
        self.workspace.save_json(f"checkpoints/{state.id}/proposal.json", proposal)
        state.status = "PROPOSAL_VALIDATED"
        self._save(state)
        if state.protected_streams:
            state.status = "AWAITING_META_APPROVAL"
            self._save(state)
            return {"ok": True, "checkpoint_id": state.id, "status": state.status, "approval_required": True, "proposal": proposal}
        state.status = "FINAL_CHANGESET_VALIDATED"
        self._save(state)
        return self._commit_and_finalize(state)

    def _protected_diff(self, operations: list[Operation]) -> list[dict[str, Any]]:
        diffs: list[dict[str, Any]] = []
        current = self.workspace.repository.current_records("meta_revisions").get("meta_current")
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
                }
            )
        return diffs

    def decide(
        self,
        decision: Literal["yes", "no"],
        *,
        reason: str | None = None,
        native_message_id: str | None = None,
    ) -> dict[str, Any]:
        state = self._load()
        ensure(state.status == "AWAITING_META_APPROVAL", "CHECKPOINT_NOT_AWAITING_APPROVAL", "approval", f"Checkpoint is {state.status}")
        ensure(state.transaction_id is not None and state.proposal_hash is not None, "CHECKPOINT_STATE_INVALID", "approval", "Missing candidate transaction")
        if decision == "yes":
            decision_record = self.archive.archive_decision(
                checkpoint_id=state.id,
                proposal_hash=state.proposal_hash,
                decision="yes",
                native_message_id=native_message_id,
            )
            self.manager.abort(state.transaction_id)
            state.decision = "yes"
            state.decision_message_id = decision_record["message_id"]
            state.archive_cursor = self.workspace.thread().archive_cursor
            frozen = self.workspace.load_json(f"checkpoints/{state.id}/frozen.json")
            frozen["base_commit"] = self.workspace.repository.head()
            self.workspace.save_json(f"checkpoints/{state.id}/frozen.json", frozen)
            persisted_result = self.workspace.load_json(f"checkpoints/{state.id}/worker-result-initial.json")
            self._prepare_candidate(
                state,
                frozen,
                WorkerResult(
                    operations=[Operation.model_validate(item) for item in persisted_result["operations"]],
                    diagnostics=persisted_result.get("diagnostics", []),
                    skill_versions=persisted_result.get("skill_versions", {}),
                    runtime_info=persisted_result.get("runtime_info", {}),
                ),
            )
            state = self._load()
            ensure(state.transaction_id is not None, "CHECKPOINT_STATE_INVALID", "approval", "Rebuilt approval transaction is missing")
            self.manager.attach_approval(
                state.transaction_id,
                checkpoint_id=state.id,
                proposal_hash_value=state.proposal_hash or "",
                decision_message_id=state.decision_message_id,
                receipt_id=f"approval_{state.id}",
            )
            state.status = "FINAL_CHANGESET_VALIDATED"
            self._save(state)
            try:
                return self._commit_and_finalize(state)
            except Exception as exc:
                self._recover(state, exc)
                raise
        ensure(reason is not None and reason.strip(), "REJECTION_REASON_REQUIRED", "approval", "No requires a reason or supplemental experience", path="/reason")
        try:
            decision_record = self.archive.archive_decision(
                checkpoint_id=state.id,
                proposal_hash=state.proposal_hash,
                decision="no",
                reason=reason,
                native_message_id=native_message_id,
            )
            state.decision = "no"
            state.decision_message_id = decision_record["message_id"]
            state.archive_cursor = self.workspace.thread().archive_cursor
            state.through_message_id = decision_record["message_id"]
            self.manager.abort(state.transaction_id)
            frozen = self.workspace.load_json(f"checkpoints/{state.id}/frozen.json")
            frozen["message_range"]["through"] = state.through_message_id
            decision_message = self.workspace.repository.current_records("messages").get(state.decision_message_id)
            ensure(decision_message is not None, "DECISION_MESSAGE_NOT_ARCHIVED", "approval", "The rejection decision was not found in canonical conversation")
            frozen["messages"].append(decision_message)
            frozen["base_commit"] = self.workspace.repository.head()
            self.workspace.save_json(f"checkpoints/{state.id}/frozen.json", frozen)
            state.status = "WORKER_RUNNING"
            self._save(state)
            handle = WorkerHandle(**(state.worker_handle or {}))
            revised = self.adapter.resume_worker(
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
            ensure(
                not any(op.stream == "meta_revisions" for op in revised.operations),
                "REJECTION_REPROPOSED_META",
                "worker_validation",
                "A rejection revision cannot propose Meta-memory again in the same checkpoint",
            )
            return self._prepare_candidate(state, frozen, revised)
        except Exception as exc:
            self._recover(state, exc)
            raise

    def _commit_and_finalize(self, state: CheckpointState) -> dict[str, Any]:
        ensure(state.transaction_id is not None, "CHECKPOINT_STATE_INVALID", "commit", "Missing transaction")
        result = self.manager.commit(state.transaction_id)
        state.commit = result["commit"]
        state.status = "MEMORY_COMMITTED"
        thread = self.workspace.thread()
        thread.last_consolidated_message_id = (
            state.decision_message_id
            if state.decision == "yes" and state.decision_message_id
            else state.through_message_id
        )
        self.workspace.save_thread(thread)
        self._save(state)
        return self._finalize_committed(state)

    def _receipt(self, state: CheckpointState) -> dict[str, Any]:
        proposal = self.workspace.load_json(f"checkpoints/{state.id}/proposal.json")
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
                "profile": f"{self.workspace.profile.name}@{self.workspace.profile.version}",
                "policy_hash": self.workspace.profile.policy_hash,
                "workflow": "consolidate@0.3.1",
                "skills": state.skill_versions,
            },
            "retry_count": state.retries,
            "error": state.error,
            "derivations": state.derivations,
            "summary": summary,
        }

    def _finalize_committed(self, state: CheckpointState) -> dict[str, Any]:
        try:
            if state.context_bundle is None:
                state.context_bundle = render_context(
                    repo_root=self.workspace.config.memory_root,
                    output_path=self.workspace.config.state_root / "context" / "current.md",
                    checkpoint_id=state.id,
                )
                self._save(state)
            if not state.context_published:
                self.adapter.publish_context(state.context_bundle)
                state.context_published = True
                state.status = "CONTEXT_PUBLISHED"
                self._save(state)
            if not state.compacted:
                self.adapter.compact()
                state.compacted = True
                state.status = "CONTEXT_COMPACTED"
                self._save(state)
        except Exception as exc:
            state.status = "COMMITTED_CONTEXT_PENDING"
            self._recover(state, exc, preserve_status=True)
            raise
        state.status = "DERIVATIONS_RUNNING"
        self._save(state)
        self._run_derivations(state)
        self._cleanup_worker(state)
        pending = any(not item.get("ok", False) for item in state.derivations.values())
        state.status = "COMMITTED_WITH_PENDING_DERIVATIONS" if pending else "DONE"
        state.completed_at = utc_now()
        self._save(state)
        receipt = self._receipt(state)
        try:
            if not state.receipt_inserted:
                self.adapter.insert_receipt(receipt)
                state.receipt_inserted = True
                self._save(state)
            self.workspace.save_json(f"checkpoints/{state.id}/receipt.json", receipt)
            self.adapter.unlock_input()
        except Exception as exc:
            state.status = "COMMITTED_CONTEXT_PENDING"
            self._recover(state, exc, preserve_status=True)
            raise
        return {"ok": True, "checkpoint_id": state.id, "status": state.status, "receipt": receipt}

    def _cleanup_worker(self, state: CheckpointState) -> None:
        if not state.worker_handle:
            return
        previous = state.derivations.get("worker_cleanup", {})
        if previous.get("ok"):
            return
        try:
            self.adapter.close_worker(WorkerHandle(**state.worker_handle))
            state.derivations["worker_cleanup"] = {"ok": True, "worker_id": state.worker_handle["id"]}
        except Exception as exc:
            state.derivations["worker_cleanup"] = {
                "ok": False,
                "pending": True,
                "worker_id": state.worker_handle["id"],
                "error": str(exc),
            }
        self._save(state)

    def _run_derivations(self, state: CheckpointState) -> None:
        config = self.workspace.config.checkpoint.derivations
        if config.index:
            try:
                state.derivations["index"] = build_index(repo_root=self.workspace.config.memory_root, indexes_root=self.workspace.config.indexes_root)
            except Exception as exc:
                state.derivations["index"] = {"ok": False, "pending": True, "error": str(exc)}
        if config.backlinks:
            try:
                state.derivations["backlinks"] = build_backlinks(
                    repo_root=self.workspace.config.memory_root,
                    output_path=self.workspace.config.state_root / "derivations" / f"backlinks-{state.commit}.json",
                )
            except Exception as exc:
                state.derivations["backlinks"] = {"ok": False, "pending": True, "error": str(exc)}
        if config.projection == "markdown":
            try:
                state.derivations["projection"] = project_markdown(repo_root=self.workspace.config.memory_root, output_root=self.workspace.config.projection_root)
            except Exception as exc:
                state.derivations["projection"] = {"ok": False, "pending": True, "error": str(exc)}
        elif config.projection == "affine":
            try:
                state.derivations["projection"] = project_affine(repo_root=self.workspace.config.memory_root, state_root=self.workspace.config.state_root)
            except Exception as exc:
                state.derivations["projection"] = {"ok": False, "pending": True, "error": str(exc)}
        self._save(state)

    def retry(self) -> dict[str, Any]:
        state = self._load()
        ensure(state.status in {"RECOVERY", "COMMITTED_CONTEXT_PENDING"}, "CHECKPOINT_NOT_RETRYABLE", "checkpoint", f"Checkpoint is {state.status}")
        state.retries += 1
        state.error = None
        state.failure_phase = None
        if state.commit:
            state.status = "MEMORY_COMMITTED"
            self._save(state)
            return self._finalize_committed(state)
        try:
            frozen = self.workspace.load_json(f"checkpoints/{state.id}/frozen.json")
            handle = WorkerHandle(**(state.worker_handle or {}))
            payload: dict[str, Any]
            if state.decision == "no":
                payload = {
                    "kind": "rejection_revision",
                    "decision_message_id": state.decision_message_id,
                    "original_proposal_hash": state.proposal_hash,
                    "requirements": ["remove all user_approval operations", "do not ask a follow-up question"],
                    "frozen": frozen,
                }
            else:
                payload = {"kind": "consolidate", "frozen": frozen, "retry": state.retries}
            state.status = "WORKER_RUNNING"
            self._save(state)
            try:
                result = self.adapter.resume_worker(handle, payload)
            except MemError as exc:
                if exc.detail.code != "HARNESS_REQUEST_FAILED":
                    raise
                replacement = self.adapter.spawn_worker(
                    {
                        "checkpoint_id": state.id,
                        "worker_id": f"worker_{uuid.uuid4().hex}",
                        "replacement_for": handle.id,
                        "frozen_input_path": str(self._checkpoint_dir(state.id) / "frozen.json"),
                    }
                )
                state.worker_handle = replacement.as_dict()
                self._save(state)
                result = self.adapter.resume_worker(replacement, payload)
            return self._prepare_candidate(state, frozen, result)
        except Exception as exc:
            self._recover(state, exc)
            raise

    def retry_derivations(self) -> dict[str, Any]:
        state = self._load()
        ensure(state.commit is not None, "CHECKPOINT_NOT_COMMITTED", "derivations", "Checkpoint has no canonical commit")
        self._run_derivations(state)
        self._cleanup_worker(state)
        pending = any(not item.get("ok", False) for item in state.derivations.values())
        state.status = "COMMITTED_WITH_PENDING_DERIVATIONS" if pending else "DONE"
        state.completed_at = utc_now()
        self._save(state)
        receipt = self._receipt(state)
        self.adapter.insert_receipt(receipt)
        self.workspace.save_json(f"checkpoints/{state.id}/receipt.json", receipt)
        self.adapter.unlock_input()
        return {"ok": True, "checkpoint_id": state.id, "status": state.status, "derivations": state.derivations, "receipt": receipt}

    def abort(self) -> dict[str, Any]:
        state = self._load()
        ensure(state.commit is None, "CHECKPOINT_ALREADY_COMMITTED", "checkpoint", "A committed checkpoint cannot be aborted; retry context publication")
        if state.transaction_id:
            transaction = self.manager.load(state.transaction_id)
            if transaction.status != "aborted":
                self.manager.abort(state.transaction_id)
        self._cleanup_worker(state)
        state.status = "ABORTED"
        self._save(state)
        self.adapter.unlock_input()
        return {"ok": True, "checkpoint_id": state.id, "status": state.status}

    def _recover(self, state: CheckpointState, exc: Exception, *, preserve_status: bool = False) -> None:
        state.failure_phase = state.status
        if isinstance(exc, MemError):
            state.error = exc.as_dict()["error"]
        else:
            state.error = {"code": "UNEXPECTED", "phase": str(state.status).lower(), "message": str(exc), "retryable": True, "recovery": ["Run /pco retry with the same checkpoint"]}
        if not preserve_status:
            state.status = "RECOVERY"
        self._save(state)
