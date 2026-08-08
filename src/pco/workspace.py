from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from mem_core.errors import MemError, ensure
from mem_core.profile import Profile
from mem_core.registry import default_registry
from mem_core.repository import MemoryRepository
from pydantic import BaseModel, ConfigDict, Field
import yaml

from .config import PCOConfig
from .paths import bundled_profile


class ThreadState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str
    active_epoch_id: str
    last_archived_message_id: str | None = None
    last_consolidated_message_id: str | None = None
    archive_cursor: str | None = None
    created_at: str


class HarnessBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    thread_id: str
    epoch_id: str
    harness: str
    native_session_id: str | None = None
    status: str = "active"


class Workspace:
    def __init__(self, config: PCOConfig) -> None:
        self.config = config
        self.root = config.workspace_root.resolve()
        self.profile_path = bundled_profile(config.profile)
        self.profile = Profile.load(self.profile_path, default_registry())
        self.repository = MemoryRepository(config.memory_root, self.profile)

    def init(self) -> dict[str, Any]:
        ensure(not self.root.exists() or not any(self.root.iterdir()), "WORKSPACE_NOT_EMPTY", "init", f"Workspace is not empty: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        for path in (self.config.state_root, self.config.indexes_root, self.config.projection_root):
            path.mkdir(parents=True, exist_ok=True)
        (self.root / "config.yaml").write_text(
            yaml.safe_dump(
                self.config.model_dump(mode="json"),
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        commit = self.repository.init(copy_profile=True)
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        thread_id = f"thread_{uuid.uuid4().hex}"
        epoch_id = f"epoch_{uuid.uuid4().hex}"
        thread = ThreadState(thread_id=thread_id, active_epoch_id=epoch_id, created_at=now)
        binding = HarnessBinding(
            id=f"binding_{uuid.uuid4().hex}",
            thread_id=thread_id,
            epoch_id=epoch_id,
            harness=self.config.harness.kind,
            native_session_id=self.config.harness.main_session_id,
        )
        self.save_json("thread.json", thread.model_dump(mode="json"))
        self.save_json("harness-binding.json", binding.model_dump(mode="json"))
        (self.config.state_root / "workers").mkdir(exist_ok=True)
        (self.config.state_root / "context").mkdir(exist_ok=True)
        (self.config.state_root / "derivations").mkdir(exist_ok=True)
        return {"ok": True, "workspace": str(self.root), "memory_commit": commit, "thread_id": thread_id, "epoch_id": epoch_id}

    def assert_initialized(self) -> None:
        ensure((self.config.state_root / "thread.json").is_file(), "WORKSPACE_NOT_INITIALIZED", "workspace", f"Not a PCO workspace: {self.root}")
        self.repository.assert_initialized()

    def state_path(self, name: str) -> Path:
        ensure(".." not in Path(name).parts and not Path(name).is_absolute(), "STATE_PATH_UNSAFE", "workspace", "Unsafe state path", value=name)
        return self.config.state_root / name

    def load_json(self, name: str) -> dict[str, Any]:
        path = self.state_path(name)
        ensure(path.is_file(), "STATE_NOT_FOUND", "workspace", f"Missing runtime state: {name}")
        return json.loads(path.read_text(encoding="utf-8"))

    def save_json(self, name: str, value: dict[str, Any]) -> None:
        path = self.state_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)

    def thread(self) -> ThreadState:
        return ThreadState.model_validate(self.load_json("thread.json"))

    def save_thread(self, state: ThreadState) -> None:
        self.save_json("thread.json", state.model_dump(mode="json"))

    def binding(self) -> HarnessBinding:
        return HarnessBinding.model_validate(self.load_json("harness-binding.json"))

    def save_binding(self, binding: HarnessBinding) -> None:
        self.save_json("harness-binding.json", binding.model_dump(mode="json"))

    def canonical_profile(self) -> Profile:
        path = self.config.memory_root / "profiles" / self.profile.name
        return Profile.load(path, default_registry())

    def refresh_repository_profile(self) -> MemoryRepository:
        canonical = self.canonical_profile()
        marker_path = self.config.memory_root / ".mem-profile.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if canonical.name != marker.get("name") or canonical.version != marker.get("version"):
            raise MemError(
                "PROFILE_MARKER_MISMATCH",
                "profile_load",
                f"Canonical Profile {canonical.name}@{canonical.version} does not match .mem-profile.json",
                path=str(marker_path),
                recovery=["Recreate the workspace", "Restore a matching Profile version"],
            )
        self.profile = canonical
        self.profile_path = self.config.memory_root / "profiles" / self.profile.name
        self.repository = MemoryRepository(self.config.memory_root, self.profile)
        return self.repository

    def doctor(self) -> dict[str, Any]:
        self.assert_initialized()
        verification = self.refresh_repository_profile().verify()
        binding = self.binding()
        thread = self.thread()
        issues: list[str] = []
        if binding.thread_id != thread.thread_id or binding.epoch_id != thread.active_epoch_id:
            issues.append("active harness binding does not match thread/epoch")
        if not verification["pre_commit_hook"]["installed"]:
            issues.append("mem-core pre-commit validation hook is not installed")
        lock = self.config.state_root / "checkpoint-lock.json"
        return {
            "ok": verification["ok"] and not issues,
            "workspace": str(self.root),
            "memory": verification,
            "thread": thread.model_dump(mode="json"),
            "binding": binding.model_dump(mode="json"),
            "checkpoint_locked": lock.exists(),
            "issues": issues,
        }
