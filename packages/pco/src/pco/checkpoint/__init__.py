from __future__ import annotations

import hashlib
import uuid
from typing import Any, Literal

from mem_core.errors import ensure
from mem_core.transaction import TransactionManager

from ..archive import ConversationArchive
from ..harness import HarnessAdapter
from ..sources import SourceManager
from ..workspace import Workspace
from . import approval as approval_steps
from . import derivations as derivations_steps
from . import finalize as finalize_steps
from . import recovery as recovery_steps
from . import state as state_store
from . import steps as checkpoint_steps
from .state import CheckpointState, CheckpointStatus, PendingCompaction

__all__ = ["CheckpointEngine", "CheckpointState", "CheckpointStatus"]


class CheckpointEngine:
    """Durable manual/auto checkpoint state machine.

    Both triggers enter ``request``; only the trigger field differs. All
    durable boundaries are persisted before the next external side effect.
    Step logic lives in the sibling modules (steps/approval/finalize/
    derivations/recovery); this class is the orchestration facade.
    """

    def __init__(self, workspace: Workspace, adapter: HarnessAdapter) -> None:
        self.workspace = workspace
        self.adapter = adapter
        self.archive = ConversationArchive(workspace)
        self.sources = SourceManager(workspace)
        self.manager = TransactionManager(workspace.repository, workspace.config.state_root)

    # Thin compatibility delegators retained for tests and internal callers.
    @property
    def active_path(self) -> Any:
        return state_store.active_path(self)

    def _checkpoint_dir(self, checkpoint_id: str) -> Any:
        return state_store.checkpoint_dir(self, checkpoint_id)

    def _save(self, state: CheckpointState) -> None:
        state_store.save(self, state)

    def _load(self) -> CheckpointState:
        return state_store.load(self)

    def _recover(self, state: CheckpointState, exc: Exception, *, preserve_status: bool = False) -> None:
        state_store.recover(self, state, exc, preserve_status=preserve_status)

    def _worker_profile_contract(self) -> dict[str, Any]:
        return checkpoint_steps.worker_profile_contract(self)

    def _freeze(self, state: CheckpointState) -> dict[str, Any]:
        return checkpoint_steps.freeze(self, state)

    def _prepare_candidate(self, state: CheckpointState, frozen: dict[str, Any], result: Any) -> dict[str, Any]:
        return checkpoint_steps.prepare_candidate(self, state, frozen, result)

    def _commit_and_finalize(self, state: CheckpointState) -> dict[str, Any]:
        return finalize_steps.commit_and_finalize(self, state)

    def _finalize_committed(self, state: CheckpointState) -> dict[str, Any]:
        return finalize_steps.finalize_committed(self, state)

    def _receipt(self, state: CheckpointState) -> dict[str, Any]:
        return finalize_steps.receipt(self, state)

    def _write_checkpoint_record(self, state: CheckpointState) -> None:
        finalize_steps.write_checkpoint_record(self, state)

    def _run_derivations(self, state: CheckpointState) -> None:
        derivations_steps.run_derivations(self, state)

    def _cleanup_worker(self, state: CheckpointState) -> None:
        derivations_steps.cleanup_worker(self, state)

    def status(self) -> dict[str, Any]:
        return state_store.status(self)

    def should_auto_checkpoint(self, intent: str = "compact") -> bool:
        return state_store.should_auto_checkpoint(self, intent)

    def request(
        self,
        trigger: Literal["manual", "auto"],
        intent: Literal["consolidate", "compact"] | None = None,
        origin: Literal["command", "idle_threshold", "harness_auto_compaction"] | None = None,
        pending_compaction: PendingCompaction | dict[str, Any] | None = None,
        native_compact_bypass: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # ``None`` is retained only for v0.3 Python callers. Trusted v0.4
        # callers must pass the intent explicitly; the Plugin never relies on
        # this compatibility default.
        legacy_call = intent is None
        intent = intent or "compact"
        ensure(intent in {"consolidate", "compact"}, "CHECKPOINT_INTENT_INVALID", "checkpoint", "Unknown checkpoint intent")
        pending_request = (
            pending_compaction
            if isinstance(pending_compaction, PendingCompaction)
            else PendingCompaction.model_validate(pending_compaction)
            if pending_compaction is not None
            else None
        )
        if pending_request is not None:
            intent = "compact"
            origin = "harness_auto_compaction"
        if trigger == "auto":
            threshold_ok = origin == "harness_auto_compaction" or self.should_auto_checkpoint(intent)
            if legacy_call:
                # Keep direct v0.3 Python callers readable during migration;
                # trusted v0.4 Plugin/CLI callers always provide an intent and
                # therefore use the split thresholds above.
                legacy_ratio = getattr(self.workspace.config.checkpoint, "trigger_ratio", 0.5)
                threshold_ok = threshold_ok or self.adapter.estimate_context_usage() >= legacy_ratio
            ensure(threshold_ok, "AUTO_THRESHOLD_NOT_REACHED", "checkpoint", "Automatic checkpoint threshold has not been reached")
        if state_store.active_path(self).exists():
            previous = state_store.load(self)
            if previous.status not in {"DONE", "COMMITTED_WITH_PENDING_DERIVATIONS", "ABORTED"}:
                if pending_request is None:
                    ensure(False, "CHECKPOINT_ACTIVE", "checkpoint", f"Checkpoint {previous.id} is still {previous.status}")
                if previous.pending_acceptance == "open":
                    state_store.merge_pending_compaction(self, previous, pending_request)
                    result = state_store.status(self)
                    result["pending_compaction_merged"] = True
                    return result
                # A closed acceptance window is immutable. Fall through and
                # create a new durable compact checkpoint instead of writing
                # the late request back into the old outcome.
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
        source_probe = self.sources.collect_diffs()
        has_new_messages = (
            thread.last_archived_message_id is not None
            and thread.last_consolidated_message_id != thread.last_archived_message_id
        )
        previous_source_hashes = self._last_consolidation_source_hashes()
        source_changed = dict(source_probe.get("source_hashes") or {}) != previous_source_hashes
        state = CheckpointState(
            id=f"ckpt_{uuid.uuid4().hex}",
            trigger=trigger,
            intent=intent,
            status="CHECKPOINT_REQUESTED",
            after_message_id=thread.last_consolidated_message_id,
            through_message_id=thread.last_archived_message_id,
            thread_id=thread.thread_id,
            harness_binding_id=binding.id,
            parent_session_id=session_id,
            archive_cursor=thread.archive_cursor,
            consolidation_cursor_before=thread.last_consolidated_message_id,
            consolidation_cursor_after=None,
            compaction_cursor_before=thread.compaction_cursor,
            compaction_requested=intent == "compact",
            compaction_status="pending" if intent == "compact" else "not_requested",
            compaction_origin=origin or ("command" if trigger == "manual" else "idle_threshold"),
            pending_compaction=pending_request,
            pending_compaction_request_ids=[pending_request.request_id] if pending_request else [],
            native_compact_attempt_id=(native_compact_bypass or {}).get("attempt_id"),
            harness_runtime=self.adapter.runtime_info(),
        )
        state.consolidation_source_hashes = dict(self._last_consolidation_source_hashes())
        state_store.transition(self, state, "CHECKPOINT_REQUESTED")
        try:
            state_store.transition(self, state, "INPUT_LOCKED")
            if not has_new_messages and not source_changed:
                state.consolidation_status = "no_op"
                state.consolidation_cursor_after = thread.last_consolidated_message_id
                # A no-op still observes the current registered-source
                # baseline. Persist it in the canonical source_hashes field;
                # otherwise the next request compares real hashes with {}
                # and incorrectly starts a source-only consolidate.
                state.source_hashes = dict(source_probe.get("source_hashes") or {})
                state.consolidation_source_hashes = dict(state.source_hashes)
                latest = self._latest_successful_checkpoint_payload()
                reusable = self._validated_reusable_context(latest)
                ensure(reusable is not None, "CONTEXT_BUNDLE_MISSING", "context", "The latest successful context cannot be safely reused")
                state.content_commit = reusable["content_commit"]
                state.commit = state.content_commit
                state.context_bundle = reusable["bundle"]
                state.context_published = True
                state.context_publication_status = "completed"
                result = finalize_steps.finalize_noop_compact(self, state)
                if state_store.load(self).pending_compaction is not None:
                    return recovery_steps.resume_pending_compaction(self, result)
                return result
            frozen = checkpoint_steps.freeze(self, state)
            state.source_hashes = dict(frozen.get("source_hashes", {}))
            state.consolidation_source_hashes = dict(frozen.get("source_hashes", {}))
            state_store.transition(self, state, "TRANSCRIPT_FROZEN")
            handle = self.adapter.spawn_worker(
                {
                    "checkpoint_id": state.id,
                    "worker_id": f"worker_{uuid.uuid4().hex}",
                    "frozen_input_path": str(state_store.checkpoint_dir(self, state.id) / "frozen.json"),
                }
            )
            state.worker_handle = handle.as_dict()
            state_store.transition(self, state, "WORKER_RUNNING")
            result = self.adapter.resume_worker(handle, {"kind": "consolidate", "frozen": frozen})
            return checkpoint_steps.prepare_candidate(self, state, frozen, result)
        except Exception as exc:
            state_store.recover(self, state, exc)
            raise

    def _last_consolidation_source_hashes(self) -> dict[str, str]:
        """Read the source-hash baseline from the latest successful receipt."""
        try:
            # ``revision`` is scoped to a checkpoint ID. The JSONL append
            # order is the global commit order and must be used when two
            # checkpoints have different revision numbers.
            records = list(self.workspace.repository.iter_records("checkpoints"))
            for record in reversed(records):
                payload = record.get("payload", {})
                if payload.get("status") in {"committed", "committed_with_pending_derivations"}:
                    return dict(payload.get("source_hashes") or {})
        except Exception:
            pass
        return {}

    def _validated_reusable_context(self, payload: dict[str, Any] | None) -> dict[str, Any] | None:
        """Return the last published context only when every hash agrees.

        ``current.json`` is a cache, not proof of publication. The checkpoint
        receipt, canonical content commit, cache metadata and rendered
        ``current.md`` must all describe the same successful context.
        """

        if not payload or payload.get("status") not in {"committed", "committed_with_pending_derivations"}:
            return None
        content_commit = payload.get("content_commit") or payload.get("git_commit")
        checkpoint_id = payload.get("checkpoint_id") or payload.get("id")
        if not isinstance(content_commit, str) or not content_commit or not isinstance(checkpoint_id, str):
            return None
        context_json_path = self.workspace.state_path("context/current.json")
        context_md_path = self.workspace.state_path("context/current.md")
        receipt_path = self.workspace.state_path(f"checkpoints/{checkpoint_id}/receipt.json")
        try:
            bundle = self.workspace.load_json("context/current.json")
            receipt = self.workspace.load_json(f"checkpoints/{checkpoint_id}/receipt.json")
        except Exception:
            return None
        if not context_json_path.is_file() or not context_md_path.is_file() or not receipt_path.is_file():
            return None
        if not isinstance(bundle, dict) or not isinstance(receipt, dict):
            return None
        content_hash = bundle.get("content_hash")
        if not isinstance(content_hash, str) or not content_hash:
            return None
        try:
            actual_hash = "sha256:" + hashlib.sha256(context_md_path.read_bytes()).hexdigest()
        except OSError:
            return None
        if actual_hash != content_hash or bundle.get("source_commit") != content_commit or bundle.get("memory_commit") != content_commit:
            return None
        publication = receipt.get("context_publication")
        if not isinstance(publication, dict) or publication.get("status") != "completed":
            return None
        if publication.get("content_hash") != content_hash:
            return None
        if receipt.get("content_commit") != content_commit and receipt.get("git_commit") != content_commit:
            return None
        return {"content_commit": content_commit, "bundle": bundle}

    def _latest_successful_checkpoint_payload(self) -> dict[str, Any] | None:
        try:
            records = list(self.workspace.repository.iter_records("checkpoints"))
            for record in reversed(records):
                payload = record.get("payload", {})
                if payload.get("status") in {"committed", "committed_with_pending_derivations"}:
                    return {**payload, "checkpoint_id": record.get("id")}
        except Exception:
            return None
        return None

    def decide(
        self,
        decision: Literal["yes", "no"],
        *,
        reason: str | None = None,
        question_request_id: str | None = None,
        approval_grant: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return approval_steps.decide(
            self,
            decision,
            reason=reason,
            question_request_id=question_request_id,
            approval_grant=approval_grant,
            session_id=session_id,
        )

    def retry(self) -> dict[str, Any]:
        return recovery_steps.retry(self)

    def retry_derivations(self) -> dict[str, Any]:
        return recovery_steps.retry_derivations(self)

    def abort(self) -> dict[str, Any]:
        return recovery_steps.abort(self)
