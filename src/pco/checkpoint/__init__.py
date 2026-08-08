from __future__ import annotations

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
from .state import CheckpointState, CheckpointStatus

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

    def should_auto_checkpoint(self) -> bool:
        return state_store.should_auto_checkpoint(self)

    def request(self, trigger: Literal["manual", "auto"]) -> dict[str, Any]:
        if trigger == "auto":
            ensure(self.should_auto_checkpoint(), "AUTO_THRESHOLD_NOT_REACHED", "checkpoint", "Automatic checkpoint threshold has not been reached")
        if state_store.active_path(self).exists():
            previous = state_store.load(self)
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
        state_store.transition(self, state, "CHECKPOINT_REQUESTED")
        try:
            state_store.transition(self, state, "INPUT_LOCKED")
            frozen = checkpoint_steps.freeze(self, state)
            state.source_hashes = dict(frozen.get("source_hashes", {}))
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

    def decide(
        self,
        decision: Literal["yes", "no"],
        *,
        reason: str | None = None,
        native_message_id: str | None = None,
    ) -> dict[str, Any]:
        return approval_steps.decide(self, decision, reason=reason, native_message_id=native_message_id)

    def retry(self) -> dict[str, Any]:
        return recovery_steps.retry(self)

    def retry_derivations(self) -> dict[str, Any]:
        return recovery_steps.retry_derivations(self)

    def abort(self) -> dict[str, Any]:
        return recovery_steps.abort(self)
