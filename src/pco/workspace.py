from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from mem_core.errors import MemError, ensure
from mem_core.models import utc_now
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

    def _migrate_canonical_profile(self) -> str | None:
        if self.profile.name != "pco":
            return None
        canonical_root = self.config.memory_root / "profiles" / "pco"
        profile_file = canonical_root / "profile.yaml"
        ensure(profile_file.is_file(), "PROFILE_NOT_FOUND", "profile_migration", f"Missing {profile_file}")
        raw = yaml.safe_load(profile_file.read_text(encoding="utf-8"))
        schema_relative = Path("profiles/pco/schemas/search-receipt.schema.json")
        stream_relative = Path("sources/search-receipts.jsonl")
        current_version = str(raw.get("version", ""))
        has_stream = "search_receipts" in raw.get("streams", {})
        has_schema = (self.config.memory_root / schema_relative).is_file()
        if current_version == self.profile.version and has_stream and has_schema:
            return None
        ensure(
            current_version == "0.3.1" and self.profile.version == "0.3.2",
            "PROFILE_MIGRATION_UNSUPPORTED",
            "profile_migration",
            f"No automatic PCO Profile migration from {current_version} to {self.profile.version}",
            recovery=["Install a supported intermediate PCO version or migrate the canonical Profile explicitly"],
        )
        self.repository.assert_clean()
        marker_path = self.config.memory_root / ".mem-profile.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        ensure(
            marker.get("name") == "pco" and marker.get("version") == current_version,
            "PROFILE_MARKER_MISMATCH",
            "profile_migration",
            "Canonical Profile marker does not match the Profile being migrated",
        )

        migration_id = f"profile_pco_0_3_2_{uuid.uuid4().hex[:12]}"
        worktree = self.config.state_root / "profile-migrations" / migration_id / "worktree"
        base_commit = self.repository.head()
        self.repository.remove_worktree(worktree)
        self.repository.add_worktree(worktree, base_commit)
        try:
            migrated_profile_file = worktree / "profiles" / "pco" / "profile.yaml"
            migrated_raw = yaml.safe_load(migrated_profile_file.read_text(encoding="utf-8"))
            migrated_raw["version"] = self.profile.version
            streams = migrated_raw.setdefault("streams", {})
            streams["search_receipts"] = dict(self.profile.raw["streams"]["search_receipts"])
            migrated_raw.setdefault("retrieval", {}).setdefault(
                "candidate_count",
                self.profile.raw.get("retrieval", {}).get("candidate_count", 200),
            )
            migrated_profile_file.write_text(
                yaml.safe_dump(migrated_raw, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            migrated_schema = worktree / schema_relative
            migrated_schema.parent.mkdir(parents=True, exist_ok=True)
            migrated_schema.write_bytes((self.profile.root / "schemas" / "search-receipt.schema.json").read_bytes())
            migrated_stream = worktree / stream_relative
            migrated_stream.parent.mkdir(parents=True, exist_ok=True)
            migrated_stream.touch(exist_ok=True)
            migrated_marker_path = worktree / ".mem-profile.json"
            migrated_marker = json.loads(migrated_marker_path.read_text(encoding="utf-8"))
            migrated_marker["version"] = self.profile.version
            migrated_marker_path.write_text(
                json.dumps(migrated_marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            migrated_profile = Profile.load(worktree / "profiles" / "pco", default_registry())
            MemoryRepository(worktree, migrated_profile).validate_all(root=worktree)
            changed_files = [
                Path(".mem-profile.json"),
                Path("profiles/pco/profile.yaml"),
                schema_relative,
                stream_relative,
            ]
            audit = {
                "id": migration_id,
                "kind": "profile_migration",
                "base_commit": base_commit,
                "profile_before": f"pco@{current_version}",
                "profile_after": f"pco@{self.profile.version}",
                "changed_files": {
                    path.as_posix(): "sha256:" + hashlib.sha256((worktree / path).read_bytes()).hexdigest()
                    for path in changed_files
                },
                "applied_at": utc_now(),
            }
            audit_path = worktree / "transactions" / "profile-migrations.jsonl"
            with audit_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(audit, ensure_ascii=False, sort_keys=True) + "\n")
            self.repository._git("add", "--", *(path.as_posix() for path in [*changed_files, Path("transactions/profile-migrations.jsonl")]), cwd=worktree)
            self.repository._git(
                "commit",
                "-m",
                "migrate canonical Profile to pco@0.3.2",
                "-m",
                "Add wrapper-authenticated search receipts before checkpoint workers can reference them.",
                cwd=worktree,
            )
            commit = self.repository._git("rev-parse", "HEAD", cwd=worktree)
            self.repository.fast_forward(commit)
            return commit
        finally:
            self.repository.remove_worktree(worktree)

    def refresh_repository_profile(self) -> MemoryRepository:
        self._migrate_canonical_profile()
        self.profile = self.canonical_profile()
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
