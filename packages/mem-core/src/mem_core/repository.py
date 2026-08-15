from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from .errors import MemError, ensure
from .models import RecordEnvelope, latest_by_id
from .profile import Profile


class MemoryRepository:
    def __init__(self, root: Path, profile: Profile) -> None:
        self.root = root.resolve()
        self.profile = profile

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        command = ["git", *args]
        env = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = source_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        result = subprocess.run(command, cwd=cwd or self.root, text=True, capture_output=True, check=False, env=env)
        if check and result.returncode:
            raise MemError(
                "GIT_FAILED",
                "git",
                result.stderr.strip() or result.stdout.strip(),
                retryable=True,
                recovery=["Run mem git verify", "Resolve the reported Git repository problem"],
            )
        return result.stdout.strip()

    @property
    def is_initialized(self) -> bool:
        return (self.root / ".git").exists()

    def init(self, *, copy_profile: bool = True) -> str:
        ensure(not self.root.exists() or not any(self.root.iterdir()), "REPO_NOT_EMPTY", "init", f"Memory directory is not empty: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        self._git("init", "-b", "main")
        self._git("config", "user.name", "PCO Memory")
        self._git("config", "user.email", "pco-memory@local")
        for stream in self.profile.config.streams.values():
            path = self.root / stream.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        for artifact_root in self.profile.config.artifact_roots:
            path = self.root / artifact_root
            if Path(artifact_root).suffix:
                path.parent.mkdir(parents=True, exist_ok=True)
            else:
                path.mkdir(parents=True, exist_ok=True)
                (path / ".gitkeep").touch()
        (self.root / "transactions").mkdir(parents=True, exist_ok=True)
        (self.root / "transactions" / "transactions.jsonl").touch()
        (self.root / ".gitignore").write_text("*.lock\n", encoding="utf-8")
        profile_location = f"profiles/{self.profile.name}" if copy_profile else str(self.profile.root)
        (self.root / ".mem-profile.json").write_text(
            json.dumps(
                {"name": self.profile.name, "version": self.profile.version, "path": profile_location},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if copy_profile:
            target = self.root / "profiles" / self.profile.name
            shutil.copytree(self.profile.root, target)
        self._git("add", ".")
        self._git("commit", "-m", f"Initialize {self.profile.name} canonical memory")
        self.install_pre_commit_hook()
        return self.head()

    def _common_git_dir(self) -> Path:
        raw = self._git("rev-parse", "--git-common-dir")
        path = Path(raw)
        return path.resolve() if path.is_absolute() else (self.root / path).resolve()

    def install_pre_commit_hook(self) -> Path:
        """Install the repository-owned final schema/semantic validation gate."""

        self.assert_initialized()
        hook = self._common_git_dir() / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        executable = shlex.quote(sys.executable)
        content = (
            "#!/bin/sh\n"
            "# managed-by: mem-core\n"
            f"exec {executable} -m mem_core.hook --repo \"$(git rev-parse --show-toplevel)\"\n"
        )
        if hook.exists() and "managed-by: mem-core" not in hook.read_text(encoding="utf-8", errors="replace"):
            raise MemError(
                "PRE_COMMIT_HOOK_CONFLICT",
                "repository",
                f"An unmanaged pre-commit hook already exists: {hook}",
                retryable=False,
                recovery=["Chain the existing hook to `python -m mem_core.hook`, then retry"],
            )
        hook.write_text(content, encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        return hook

    def pre_commit_hook_status(self) -> dict[str, Any]:
        hook = self._common_git_dir() / "hooks" / "pre-commit"
        installed = hook.is_file() and "managed-by: mem-core" in hook.read_text(encoding="utf-8", errors="replace")
        return {"installed": installed, "path": str(hook)}

    def assert_initialized(self) -> None:
        ensure(self.is_initialized, "REPO_NOT_INITIALIZED", "repository", f"Not a Git memory repository: {self.root}")

    def head(self) -> str:
        self.assert_initialized()
        return self._git("rev-parse", "HEAD")

    def is_clean(self) -> bool:
        self.assert_initialized()
        return not self._git("status", "--porcelain")

    def assert_clean(self) -> None:
        ensure(
            self.is_clean(),
            "CANONICAL_DIRTY",
            "repository",
            "Canonical memory has uncommitted changes",
            retryable=False,
            recovery=["Inspect and commit or move manual changes before retrying"],
        )

    def iter_records(self, stream_name: str, *, root: Path | None = None) -> Iterable[dict[str, Any]]:
        path = self.profile.stream_path(root or self.root, stream_name)
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise MemError(
                        "JSONL_INVALID",
                        "envelope_validation",
                        str(exc),
                        stream=stream_name,
                        path=f"{path}:{line_number}",
                    ) from exc

    def records_by_stream(self, *, root: Path | None = None) -> dict[str, list[dict[str, Any]]]:
        return {name: list(self.iter_records(name, root=root)) for name in self.profile.config.streams}

    def current_records(self, stream_name: str, *, root: Path | None = None) -> dict[str, dict[str, Any]]:
        return latest_by_id(self.iter_records(stream_name, root=root))

    def record_history(self, stream_name: str, record_id: str) -> list[dict[str, Any]]:
        return [record for record in self.iter_records(stream_name) if record.get("id") == record_id]

    def append_record(self, root: Path, stream_name: str, record: dict[str, Any]) -> None:
        path = self.profile.stream_path(root, stream_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def validate_all(self, *, root: Path | None = None) -> dict[str, Any]:
        target = root or self.root
        all_records: dict[str, list[dict[str, Any]]] = {}
        record_count = 0
        for stream_name in self.profile.config.streams:
            records = list(self.iter_records(stream_name, root=target))
            all_records[stream_name] = records
            revisions: dict[str, int] = {}
            for record in records:
                try:
                    envelope = RecordEnvelope.model_validate(record)
                except Exception as exc:
                    raise MemError(
                        "ENVELOPE_INVALID",
                        "envelope_validation",
                        str(exc),
                        stream=stream_name,
                        record_id=record.get("id"),
                    ) from exc
                expected = revisions.get(envelope.id, 0) + 1
                ensure(
                    envelope.revision == expected,
                    "REVISION_SEQUENCE_INVALID",
                    "revision_validation",
                    f"Expected revision {expected}, got {envelope.revision}",
                    stream=stream_name,
                    record_id=envelope.id,
                    path="/revision",
                    value=envelope.revision,
                )
                revisions[envelope.id] = envelope.revision
                self.profile.validate_record_schema(stream_name, record)
                record_count += 1
        self.profile.run_validators(target, all_records)
        return {"ok": True, "profile": f"{self.profile.name}@{self.profile.version}", "records": record_count}

    def add_worktree(self, path: Path, base_commit: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "--detach", str(path), base_commit)

    def remove_worktree(self, path: Path) -> None:
        if path.exists():
            self._git("worktree", "remove", "--force", str(path), check=False)
        self._git("worktree", "prune", check=False)

    def fast_forward(self, commit: str) -> None:
        self.assert_clean()
        self._git("merge", "--ff-only", commit)

    def verify(self) -> dict[str, Any]:
        self.assert_initialized()
        branch = self._git("branch", "--show-current")
        fsck = self._git("fsck", "--no-dangling")
        validation = self.validate_all()
        hook = self.pre_commit_hook_status()
        return {
            "ok": hook["installed"],
            "branch": branch,
            "head": self.head(),
            "clean": self.is_clean(),
            "fsck": fsck or "ok",
            "validation": validation,
            "pre_commit_hook": hook,
        }
