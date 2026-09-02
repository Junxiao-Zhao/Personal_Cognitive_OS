from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mem_core.repository import MemoryRepository

V05_HISTORY_PATHS = [
    "evidence/evidence.jsonl",
    "sources/sources.jsonl",
    "events/events.jsonl",
    "psychologies/psychologies.jsonl",
    "philosophies/philosophies.jsonl",
    "archetypes/archetypes.jsonl",
    "hypotheses/hypotheses.jsonl",
    "meta_revisions/meta_revisions.jsonl",
    "session_handoffs/session_handoffs.jsonl",
    "transactions/transactions.jsonl",
]


def _commit(
    repository: MemoryRepository, message: str, *, no_verify: bool = False
) -> subprocess.CompletedProcess[str]:
    command = ["git", "commit"]
    if no_verify:
        command.append("--no-verify")
    command.extend(["-m", message])
    return subprocess.run(
        command,
        cwd=repository.root,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("relative", V05_HISTORY_PATHS)
def test_hook_rejects_changed_or_deleted_v05_history(
    v05_repository: MemoryRepository, relative: str
) -> None:
    target = v05_repository.root / relative
    target.write_text('{"seed":true}\n', encoding="utf-8")
    v05_repository._git("add", relative)
    seeded = _commit(v05_repository, "seed history", no_verify=True)
    assert seeded.returncode == 0

    target.write_text("", encoding="utf-8")
    v05_repository._git("add", relative)
    result = _commit(v05_repository, "tamper")

    assert result.returncode != 0
    assert "APPEND_ONLY_VIOLATION" in result.stdout + result.stderr


@pytest.mark.parametrize("replacement", ["workspace_replaced", None])
def test_hook_rejects_changed_or_deleted_workspace_identity(
    v05_repository: MemoryRepository, replacement: str | None
) -> None:
    metadata = v05_repository.root / "workspace.json"
    metadata.write_text(
        '{"profile_version":"0.5.0","workspace_format_version":"0.5.0",'
        '"workspace_id":"workspace_original"}\n',
        encoding="utf-8",
    )
    v05_repository._git("add", "workspace.json")
    v05_repository._git("commit", "--amend", "--no-verify", "--no-edit")

    if replacement is None:
        metadata.unlink()
    else:
        metadata.write_text(
            '{"profile_version":"0.5.0","workspace_format_version":"0.5.0",'
            f'"workspace_id":"{replacement}"}}\n',
            encoding="utf-8",
        )
    v05_repository._git("add", "workspace.json")
    result = _commit(v05_repository, "replace workspace identity")

    assert result.returncode != 0
    assert "WORKSPACE_INTEGRITY_ERROR" in result.stdout + result.stderr


def test_hook_anchors_workspace_identity_to_root_commit(
    v05_repository: MemoryRepository,
) -> None:
    metadata = v05_repository.root / "workspace.json"
    metadata.write_text(
        '{"profile_version":"0.5.0","workspace_format_version":"0.5.0",'
        '"workspace_id":"workspace_original"}\n',
        encoding="utf-8",
    )
    v05_repository._git("add", "workspace.json")
    v05_repository._git("commit", "--amend", "--no-verify", "--no-edit")

    metadata.write_text(
        '{"profile_version":"0.5.0","workspace_format_version":"0.5.0",'
        '"workspace_id":"workspace_bypassed"}\n',
        encoding="utf-8",
    )
    v05_repository._git("add", "workspace.json")
    bypassed = _commit(v05_repository, "bypass identity guard", no_verify=True)
    assert bypassed.returncode == 0

    result = subprocess.run(
        [v05_repository.pre_commit_hook_status()["path"]],
        cwd=v05_repository.root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "WORKSPACE_INTEGRITY_ERROR" in result.stdout + result.stderr
