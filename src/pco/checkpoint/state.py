from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from mem_core.errors import MemError, ensure
from mem_core.models import utc_now
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from ..harness import HarnessAdapter
    from ..workspace import Workspace


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
    "INPUT_UNLOCKED",
    "DERIVATIONS_RUNNING",
    "DONE",
    "COMMITTED_WITH_PENDING_DERIVATIONS",
    "RECOVERY",
    "ABORTED",
]


TERMINAL_STATUSES = {"DONE", "COMMITTED_WITH_PENDING_DERIVATIONS", "ABORTED"}


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
    input_unlocked: bool = False
    retries: int = 0
    failure_phase: str | None = None
    error: dict[str, Any] | None = None
    derivations: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    completed_at: str | None = None


def active_path(engine: Any) -> Path:
    return engine.workspace.config.state_root / "active-checkpoint.json"


def checkpoint_dir(engine: Any, checkpoint_id: str) -> Path:
    return engine.workspace.config.state_root / "checkpoints" / checkpoint_id


def save(engine: Any, state: CheckpointState) -> None:
    state.updated_at = utc_now()
    engine.workspace.save_json("active-checkpoint.json", state.model_dump(mode="json"))
    engine.workspace.save_json(f"checkpoints/{state.id}/state.json", state.model_dump(mode="json"))
    if not state.input_unlocked and state.status not in TERMINAL_STATUSES:
        engine.adapter.lock_input(state.id, state.status)


def load(engine: Any) -> CheckpointState:
    ensure(active_path(engine).is_file(), "CHECKPOINT_NOT_FOUND", "checkpoint", "There is no active checkpoint")
    return CheckpointState.model_validate(engine.workspace.load_json("active-checkpoint.json"))


def status(engine: Any) -> dict[str, Any]:
    if not active_path(engine).exists():
        return {"ok": True, "active": False}
    state = load(engine)
    return {
        "ok": True,
        "active": state.status not in TERMINAL_STATUSES,
        "checkpoint": state.model_dump(mode="json"),
    }


def should_auto_checkpoint(engine: Any) -> bool:
    if active_path(engine).exists():
        state = load(engine)
        if state.status not in TERMINAL_STATUSES:
            return False
    return engine.adapter.estimate_context_usage() >= engine.workspace.config.checkpoint.trigger_ratio


def recover(engine: Any, state: CheckpointState, exc: Exception, *, preserve_status: bool = False) -> None:
    state.failure_phase = state.status
    if isinstance(exc, MemError):
        state.error = exc.as_dict()["error"]
    else:
        state.error = {
            "code": "UNEXPECTED",
            "phase": str(state.status).lower(),
            "message": str(exc),
            "retryable": True,
            "recovery": ["Run /pco retry with the same checkpoint"],
        }
    if not preserve_status:
        state.status = "RECOVERY"
    save(engine, state)
