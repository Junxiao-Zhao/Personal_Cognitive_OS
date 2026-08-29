from __future__ import annotations

from typing import Any

from ..harness import WorkerHandle
from .errors import failed_attempt, result_attempt, successful_attempt
from . import state as state_store
from .state import CheckpointState


def _with_derivation_status(result: dict[str, Any], status: str) -> dict[str, Any]:
    """Expose a durable phase result without changing the checkpoint model."""

    result["status"] = status
    if status == "pending":
        result["pending"] = True
    else:
        result["pending"] = False
    return result


def _pending_attempt(previous: dict[str, Any], phase: str) -> dict[str, Any]:
    """Persist the boundary before invoking a replaceable capability."""

    pending = dict(previous)
    pending["ok"] = False
    pending["pending"] = True
    pending["status"] = "pending"
    pending["phase"] = phase
    return pending


def _begin(engine: Any, state: CheckpointState, name: str) -> dict[str, Any]:
    previous = state.derivations.get(name, {})
    if not isinstance(previous, dict):
        previous = {}
    state.derivations[name] = _pending_attempt(previous, name)
    state_store.save(engine, state)
    return previous


def _finish(engine: Any, state: CheckpointState, name: str, result: dict[str, Any]) -> None:
    state.derivations[name] = result
    state_store.save(engine, state)


def cleanup_worker(engine: Any, state: CheckpointState) -> None:
    if not state.worker_handle:
        return
    previous = state.derivations.get("worker_cleanup", {})
    if previous.get("ok"):
        return
    previous = _begin(engine, state, "worker_cleanup")
    try:
        engine.adapter.close_worker(WorkerHandle(**state.worker_handle))
        _finish(engine, state, "worker_cleanup", _with_derivation_status(
            successful_attempt(
                {**previous, "worker_id": state.worker_handle["id"]},
                {"worker_id": state.worker_handle["id"]},
            ),
            "completed",
        ))
    except Exception as exc:
        failed = _with_derivation_status(
            failed_attempt({**previous, "worker_id": state.worker_handle["id"]}, exc, "worker_cleanup"),
            "failed",
        )
        # A failed cleanup is retryable work, not a terminal derivation
        # result. Keep the pending bit explicit in every receipt snapshot.
        failed["pending"] = True
        _finish(engine, state, "worker_cleanup", failed)


def run_derivations(engine: Any, state: CheckpointState) -> None:
    config = engine.workspace.config.checkpoint.derivations
    source_commit = state.content_commit or state.commit
    if config.index and not state.derivations.get("index", {}).get("ok"):
        previous = _begin(engine, state, "index")
        try:
            result = engine.workspace.profile.invoke(
                "index.build",
                repo_root=engine.workspace.config.memory_root,
                indexes_root=engine.workspace.config.indexes_root,
                source_commit=source_commit,
            )
            _finish(engine, state, "index", _with_derivation_status(
                result_attempt(previous, result, "index"),
                "completed" if result.get("ok", True) else "failed",
            ))
        except Exception as exc:
            _finish(engine, state, "index", _with_derivation_status(failed_attempt(previous, exc, "index"), "failed"))
    if config.backlinks and not state.derivations.get("backlinks", {}).get("ok"):
        previous = _begin(engine, state, "backlinks")
        try:
            result = engine.workspace.profile.invoke(
                "backlinks.build",
                repo_root=engine.workspace.config.memory_root,
                output_path=engine.workspace.config.state_root / "derivations" / f"backlinks-{source_commit}.json",
                source_commit=source_commit,
            )
            _finish(engine, state, "backlinks", _with_derivation_status(
                result_attempt(previous, result, "backlinks"),
                "completed" if result.get("ok", True) else "failed",
            ))
        except Exception as exc:
            _finish(engine, state, "backlinks", _with_derivation_status(failed_attempt(previous, exc, "backlinks"), "failed"))
    if config.projection == "markdown" and not state.derivations.get("projection", {}).get("ok"):
        previous = _begin(engine, state, "projection")
        try:
            result = engine.workspace.profile.invoke(
                "projections.markdown",
                repo_root=engine.workspace.config.memory_root,
                output_root=engine.workspace.config.projection_root,
                source_commit=source_commit,
            )
            _finish(engine, state, "projection", _with_derivation_status(
                result_attempt(previous, result, "projection"),
                "completed" if result.get("ok", True) else "failed",
            ))
        except Exception as exc:
            _finish(engine, state, "projection", _with_derivation_status(failed_attempt(previous, exc, "projection"), "failed"))
    elif config.projection == "affine" and not state.derivations.get("projection", {}).get("ok"):
        previous = _begin(engine, state, "projection")
        try:
            result = engine.workspace.profile.invoke(
                "projections.affine",
                repo_root=engine.workspace.config.memory_root,
                state_root=engine.workspace.config.state_root,
                source_commit=source_commit,
            )
            _finish(engine, state, "projection", _with_derivation_status(
                result_attempt(previous, result, "projection"),
                "completed" if result.get("ok", True) else "failed",
            ))
        except Exception as exc:
            _finish(engine, state, "projection", _with_derivation_status(failed_attempt(previous, exc, "projection"), "failed"))
