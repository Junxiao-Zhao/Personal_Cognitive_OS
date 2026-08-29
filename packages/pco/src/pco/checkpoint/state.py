from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
import fcntl
from typing import TYPE_CHECKING, Any, Literal

from mem_core.errors import MemError, ensure
from mem_core.models import utc_now
from pydantic import BaseModel, ConfigDict, Field, model_validator

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

CheckpointIntent = Literal["consolidate", "compact"]
ConsolidationStatus = Literal["pending", "no_op", "committed"]
ContextPublicationStatus = Literal["pending", "completed", "failed"]
CompactionStatus = Literal["not_requested", "pending", "completed", "failed"]
CompactionOrigin = Literal["command", "idle_threshold", "harness_auto_compaction"]


class PendingCompaction(BaseModel):
    """A durable request waiting behind an active checkpoint boundary."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    event_id: str | None = None
    session_id: str
    requested_boundary: str | dict[str, Any] | None = None
    requested_at: str | float
    origin: CompactionOrigin


TERMINAL_STATUSES = {"DONE", "COMMITTED_WITH_PENDING_DERIVATIONS", "ABORTED"}


class CheckpointState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_state(cls, value: Any) -> Any:
        return migrate_checkpoint_state(value)

    id: str
    trigger: Literal["manual", "auto"]
    # The pre-v0.4 checkpoint implementation always compacted after commit.
    # This default is intentionally only a model-compatibility default; new
    # trusted callers must persist the requested intent explicitly.
    intent: CheckpointIntent = "compact"
    status: CheckpointStatus
    after_message_id: str | None
    through_message_id: str | None
    thread_id: str
    harness_binding_id: str
    parent_session_id: str
    archive_cursor: str | None = None
    consolidation_cursor_before: str | None = None
    consolidation_cursor_after: str | None = None
    compaction_cursor_before: str | None = None
    compaction_cursor_after: str | None = None
    source_hashes: dict[str, str] = Field(default_factory=dict)
    # ``source_hashes`` is the frozen input snapshot. This field is the
    # baseline belonging to the last successful consolidation.
    consolidation_source_hashes: dict[str, str] = Field(default_factory=dict)
    harness_runtime: dict[str, Any] = Field(default_factory=dict)
    operation_counts: dict[str, int] = Field(default_factory=dict)
    skill_versions: dict[str, str] = Field(default_factory=dict)
    meta_revision: int | None = None
    continuation_revision: int | None = None
    transaction_id: str | None = None
    transaction_fingerprint: str | None = None
    proposal_hash: str | None = None
    transaction_proposal_hash: str | None = None
    approval_challenge_id: str | None = None
    approval_receipt_id: str | None = None
    protected_streams: list[str] = Field(default_factory=list)
    promotion_proposal_hash: str | None = None
    promotion_protected_streams: list[str] = Field(default_factory=list)
    worker_handle: dict[str, str] | None = None
    decision: Literal["yes", "no"] | None = None
    decision_message_id: str | None = None
    decision_question_request_id: str | None = None
    decision_authorization_id: str | None = None
    # ``commit`` is retained as a compatibility alias for content_commit.
    content_commit: str | None = None
    audit_commit: str | None = None
    audit_transaction_id: str | None = None
    commit: str | None = None
    context_bundle: dict[str, Any] | None = None
    context_published: bool = False
    consolidation_status: ConsolidationStatus = "pending"
    context_publication_status: ContextPublicationStatus = "pending"
    compaction_requested: bool = False
    compaction_status: CompactionStatus = "not_requested"
    compaction_origin: CompactionOrigin | None = None
    pending_compaction: PendingCompaction | None = None
    pending_compaction_request_ids: list[str] = Field(default_factory=list)
    native_compact_attempt_id: str | None = None
    # Retained as a read/write compatibility field for the pre-v0.4 runtime.
    # It is not sufficient to determine recovery semantics on its own.
    compacted: bool = False
    receipt_inserted: bool = False
    # Receipt publication is a generation-aware two-phase boundary.  The
    # boolean above remains for old state files, but never suppresses a newer
    # final generation after a crash or a late pending compact.
    pending_acceptance: Literal["open", "closed"] = "open"
    receipt_generation: int = 0
    host_receipt_generation: int | None = None
    receipt_kind: Literal["intermediate", "final"] = "final"
    receipt_outbox: dict[str, Any] | None = None
    input_unlocked: bool = False
    retries: int = 0
    failure_phase: str | None = None
    error: dict[str, Any] | None = None
    derivations: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    completed_at: str | None = None


def migrate_checkpoint_state(value: Any) -> Any:
    """Normalize v0.3 checkpoint JSON before Pydantic validation.

    Legacy state had one implicit operation (consolidate followed by native
    compact), one archive cursor, and a ``compacted`` flag. The migration
    preserves that behavior for old callers while making the new durable
    boundaries explicit. Cursor advancement is never invented for an old
    state unless its corresponding success boundary is observable.
    """

    if isinstance(value, CheckpointState):
        return value
    if not isinstance(value, dict):
        return value

    data = dict(value)
    legacy = "intent" not in data
    status = data.get("status")
    compacted = bool(data.get("compacted", False))
    committed_statuses = {
        "MEMORY_COMMITTED",
        "COMMITTED_CONTEXT_PENDING",
        "CONTEXT_PUBLISHED",
        "CONTEXT_COMPACTED",
        "RECEIPT_INSERTED",
        "INPUT_UNLOCKED",
        "DERIVATIONS_RUNNING",
        "DONE",
        "COMMITTED_WITH_PENDING_DERIVATIONS",
    }

    if legacy:
        # v0.3 had no consolidate-only intent: every checkpoint entered the
        # native compact phase after memory commit. Keeping this inference
        # explicit makes old active and completed states readable without
        # treating ``compacted`` as the new recovery state machine.
        data["intent"] = "compact"

    data.setdefault("consolidation_cursor_before", data.get("after_message_id"))
    if "consolidation_cursor_after" not in data:
        if data.get("content_commit") or status in committed_statuses:
            data["consolidation_cursor_after"] = data.get("through_message_id")
        else:
            data["consolidation_cursor_after"] = None
    data.setdefault("compaction_cursor_before", None)
    # A legacy ``compacted`` flag proves only that an old side effect was
    # recorded; it does not prove the durable message boundary. Keep the new
    # cursor unknown unless an explicit, successful boundary was persisted.
    data.setdefault("compaction_cursor_after", None)

    if "consolidation_source_hashes" not in data:
        data["consolidation_source_hashes"] = (
            dict(data.get("source_hashes") or {}) if data.get("consolidation_cursor_after") else {}
        )

    if "consolidation_status" not in data:
        data["consolidation_status"] = "committed" if data.get("consolidation_cursor_after") else "pending"
    if "context_publication_status" not in data:
        data["context_publication_status"] = (
            "completed" if data.get("context_published") else "pending"
        )
    if "compaction_requested" not in data:
        data["compaction_requested"] = legacy or data.get("intent") == "compact"
    if "compaction_status" not in data:
        data["compaction_status"] = (
            "completed" if compacted else "pending" if data["compaction_requested"] else "not_requested"
        )
    data.setdefault("compaction_origin", None)
    data.setdefault("pending_compaction", None)
    data.setdefault(
        "pending_compaction_request_ids",
        [data["pending_compaction"]["request_id"]]
        if isinstance(data.get("pending_compaction"), dict) and data["pending_compaction"].get("request_id")
        else [],
    )
    data.setdefault("native_compact_attempt_id", None)
    data.setdefault("pending_acceptance", "open")
    data.setdefault("receipt_generation", 0)
    data.setdefault(
        "host_receipt_generation",
        0 if data.get("receipt_inserted") is True else None,
    )
    data.setdefault("receipt_kind", "final")
    data.setdefault("receipt_outbox", None)
    return data


def active_path(engine: Any) -> Path:
    return engine.workspace.config.state_root / "active-checkpoint.json"


def checkpoint_dir(engine: Any, checkpoint_id: str) -> Path:
    return engine.workspace.config.state_root / "checkpoints" / checkpoint_id


def save(engine: Any, state: CheckpointState) -> None:
    state.updated_at = utc_now()
    engine.workspace.save_json("active-checkpoint.json", state.model_dump(mode="json"))
    engine.workspace.save_json(f"checkpoints/{state.id}/state.json", state.model_dump(mode="json"))


@contextmanager
def operation_lock(engine: Any) -> Iterator[None]:
    """Serialize pending acceptance and receipt publication for one workspace.

    The lock is deliberately a small OS-level lock file rather than an
    in-memory mutex: Harness hooks and CLI retries are separate processes.
    All state writes remain atomic JSON replacements, while this lock gives
    the pending merge/final receipt decision one linearization point.
    """

    path = engine.workspace.state_path("checkpoint-operation.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def merge_pending_compaction(engine: Any, state: CheckpointState, pending: PendingCompaction | dict[str, Any]) -> bool:
    """Attach a Harness compaction request to the one durable checkpoint.

    The active checkpoint remains authoritative: a pending compact never
    changes its original intent or creates a second active state. Repeated
    event/request IDs are retained as audit associations but do not replace
    the first request's boundary.
    """

    request = pending if isinstance(pending, PendingCompaction) else PendingCompaction.model_validate(pending)
    with operation_lock(engine):
        # Never merge into a stale object that a finalizer may have already
        # persisted. Reloading here prevents a late hook from being lost by a
        # subsequent save of the caller's old snapshot.
        state = load(engine)
        ensure(
            state.pending_acceptance == "open",
            "CHECKPOINT_ACCEPTANCE_CLOSED",
            "checkpoint",
            "The checkpoint final outcome is already frozen; create a new compact attempt",
            retryable=True,
        )
        request_ids = list(state.pending_compaction_request_ids)
        changed = False
        if request.request_id not in request_ids:
            request_ids.append(request.request_id)
            state.pending_compaction_request_ids = request_ids
            changed = True
        if state.pending_compaction is None:
            state.pending_compaction = request
            changed = True
        elif state.pending_compaction.request_id != request.request_id:
            # Keep the first durable boundary. The additional request ID is
            # the replay/duplicate association used during recovery.
            changed = True

        state.compaction_requested = True
        if state.compaction_status == "not_requested":
            state.compaction_status = "pending"
        if state.compaction_origin is None:
            state.compaction_origin = request.origin
        if state.status in {"DONE", "COMMITTED_WITH_PENDING_DERIVATIONS"}:
            state.input_unlocked = False
            state.status = "COMMITTED_CONTEXT_PENDING"
            changed = True
        if changed:
            transition(engine, state, state.status)
        return changed


def retire_pending_compaction(engine: Any, state: CheckpointState) -> None:
    """Retire pending Harness requests only after compact receipt/unlock."""

    ensure(
        state.pending_compaction is not None,
        "PENDING_COMPACTION_NOT_FOUND",
        "checkpoint",
        "Checkpoint has no pending Harness compaction",
    )
    ensure(
        state.compaction_status == "completed" and state.receipt_inserted and state.input_unlocked,
        "PENDING_COMPACTION_NOT_COMPLETE",
        "checkpoint",
        "Pending Harness compaction cannot retire before compact, receipt, and unlock succeed",
    )
    state.pending_compaction = None
    save(engine, state)


def transition(engine: Any, state: CheckpointState, new_status: str) -> None:
    """Persist a status change and maintain the external input lock."""
    state.status = new_status
    if not state.input_unlocked and state.status not in TERMINAL_STATUSES:
        engine.adapter.lock_input(state.id, state.status)
    save(engine, state)


def load(engine: Any) -> CheckpointState:
    ensure(active_path(engine).is_file(), "CHECKPOINT_NOT_FOUND", "checkpoint", "There is no active checkpoint")
    return CheckpointState.model_validate(
        migrate_checkpoint_state(engine.workspace.load_json("active-checkpoint.json"))
    )


def status(engine: Any) -> dict[str, Any]:
    if not active_path(engine).exists():
        return {"ok": True, "active": False}
    state = load(engine)
    result = {
        "ok": True,
        "active": state.status not in TERMINAL_STATUSES,
        "checkpoint": state.model_dump(mode="json"),
        "thread_runtime": {
            "consolidation_cursor": engine.workspace.thread().last_consolidated_message_id,
            "compaction_cursor": engine.workspace.thread().compaction_cursor,
        },
        "canonical_transaction": {
            "created": bool(state.transaction_id and state.consolidation_status != "no_op"),
            "transaction_id": state.transaction_id if state.consolidation_status != "no_op" else None,
        },
    }
    proposal_path = checkpoint_dir(engine, state.id) / "proposal.json"
    if proposal_path.is_file() and state.status == "AWAITING_META_APPROVAL":
        result["proposal"] = engine.workspace.load_json(f"checkpoints/{state.id}/proposal.json")
    return result


def should_auto_checkpoint(engine: Any, intent: str = "compact") -> bool:
    if active_path(engine).exists():
        state = load(engine)
        if state.status not in TERMINAL_STATUSES:
            return False
    checkpoint = engine.workspace.config.checkpoint
    if intent == "compact":
        return bool(checkpoint.auto_compact.enabled and engine.adapter.estimate_context_usage() >= checkpoint.auto_compact.context_ratio)
    if not checkpoint.auto_consolidate.enabled:
        return False
    thread = engine.workspace.thread()
    messages = list(engine.workspace.repository.iter_records("messages"))
    started = thread.last_consolidated_message_id is None
    estimated = 0.0
    for message in messages:
        if not started:
            if message.get("id") == thread.last_consolidated_message_id:
                started = True
            continue
        payload = message.get("payload", {})
        if payload.get("role") not in {"user", "assistant"}:
            continue
        estimated += sum(1 if ord(char) > 127 else 0.25 for char in str(payload.get("content", "")))
    return estimated >= checkpoint.auto_consolidate.new_public_tokens


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
            "recovery": ["Run /pco-retry with the same checkpoint"],
        }
    if not preserve_status:
        state.status = "RECOVERY"
    transition(engine, state, state.status)
