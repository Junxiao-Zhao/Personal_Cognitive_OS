from __future__ import annotations

from typing import Any

from ..harness import WorkerHandle
from .errors import failed_attempt, result_attempt, successful_attempt
from . import state as state_store
from .state import CheckpointState


def cleanup_worker(engine: Any, state: CheckpointState) -> None:
    if not state.worker_handle:
        return
    previous = state.derivations.get("worker_cleanup", {})
    if previous.get("ok"):
        return
    try:
        engine.adapter.close_worker(WorkerHandle(**state.worker_handle))
        state.derivations["worker_cleanup"] = successful_attempt(
            {**previous, "worker_id": state.worker_handle["id"]},
            {"worker_id": state.worker_handle["id"]},
        )
    except Exception as exc:
        state.derivations["worker_cleanup"] = failed_attempt(
            {**previous, "worker_id": state.worker_handle["id"]}, exc, "worker_cleanup"
        )
    state_store.save(engine, state)


def run_derivations(engine: Any, state: CheckpointState) -> None:
    config = engine.workspace.config.checkpoint.derivations
    source_commit = state.content_commit or state.commit
    if config.index and not state.derivations.get("index", {}).get("ok"):
        try:
            result = engine.workspace.profile.invoke(
                "index.build",
                repo_root=engine.workspace.config.memory_root,
                indexes_root=engine.workspace.config.indexes_root,
                source_commit=source_commit,
            )
            state.derivations["index"] = result_attempt(state.derivations.get("index", {}), result, "index")
        except Exception as exc:
            state.derivations["index"] = failed_attempt(state.derivations.get("index", {}), exc, "index")
    if config.backlinks and not state.derivations.get("backlinks", {}).get("ok"):
        try:
            result = engine.workspace.profile.invoke(
                "backlinks.build",
                repo_root=engine.workspace.config.memory_root,
                output_path=engine.workspace.config.state_root / "derivations" / f"backlinks-{source_commit}.json",
                source_commit=source_commit,
            )
            state.derivations["backlinks"] = result_attempt(state.derivations.get("backlinks", {}), result, "backlinks")
        except Exception as exc:
            state.derivations["backlinks"] = failed_attempt(state.derivations.get("backlinks", {}), exc, "backlinks")
    if config.projection == "markdown" and not state.derivations.get("projection", {}).get("ok"):
        try:
            result = engine.workspace.profile.invoke(
                "projections.markdown",
                repo_root=engine.workspace.config.memory_root,
                output_root=engine.workspace.config.projection_root,
                source_commit=source_commit,
            )
            state.derivations["projection"] = result_attempt(state.derivations.get("projection", {}), result, "projection")
        except Exception as exc:
            state.derivations["projection"] = failed_attempt(state.derivations.get("projection", {}), exc, "projection")
    elif config.projection == "affine" and not state.derivations.get("projection", {}).get("ok"):
        try:
            result = engine.workspace.profile.invoke(
                "projections.affine",
                repo_root=engine.workspace.config.memory_root,
                state_root=engine.workspace.config.state_root,
                source_commit=source_commit,
            )
            state.derivations["projection"] = result_attempt(state.derivations.get("projection", {}), result, "projection")
        except Exception as exc:
            state.derivations["projection"] = failed_attempt(state.derivations.get("projection", {}), exc, "projection")
    state_store.save(engine, state)
