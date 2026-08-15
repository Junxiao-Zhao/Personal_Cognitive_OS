from __future__ import annotations

from typing import Any

from ..backlinks import build as build_backlinks
from ..harness import WorkerHandle
from ..projections import project_affine, project_markdown
from ..retrieval import build_index
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
        state.derivations["worker_cleanup"] = {"ok": True, "worker_id": state.worker_handle["id"]}
    except Exception as exc:
        state.derivations["worker_cleanup"] = {
            "ok": False,
            "pending": True,
            "worker_id": state.worker_handle["id"],
            "error": str(exc),
        }
    state_store.save(engine, state)


def run_derivations(engine: Any, state: CheckpointState) -> None:
    config = engine.workspace.config.checkpoint.derivations
    if config.index and not state.derivations.get("index", {}).get("ok"):
        try:
            state.derivations["index"] = build_index(
                repo_root=engine.workspace.config.memory_root,
                indexes_root=engine.workspace.config.indexes_root,
            )
        except Exception as exc:
            state.derivations["index"] = {"ok": False, "pending": True, "error": str(exc)}
    if config.backlinks and not state.derivations.get("backlinks", {}).get("ok"):
        try:
            state.derivations["backlinks"] = build_backlinks(
                repo_root=engine.workspace.config.memory_root,
                output_path=engine.workspace.config.state_root / "derivations" / f"backlinks-{state.commit}.json",
            )
        except Exception as exc:
            state.derivations["backlinks"] = {"ok": False, "pending": True, "error": str(exc)}
    if config.projection == "markdown" and not state.derivations.get("projection", {}).get("ok"):
        try:
            state.derivations["projection"] = project_markdown(
                repo_root=engine.workspace.config.memory_root,
                output_root=engine.workspace.config.projection_root,
            )
        except Exception as exc:
            state.derivations["projection"] = {"ok": False, "pending": True, "error": str(exc)}
    elif config.projection == "affine" and not state.derivations.get("projection", {}).get("ok"):
        try:
            state.derivations["projection"] = project_affine(
                repo_root=engine.workspace.config.memory_root,
                state_root=engine.workspace.config.state_root,
            )
        except Exception as exc:
            state.derivations["projection"] = {"ok": False, "pending": True, "error": str(exc)}
    state_store.save(engine, state)
