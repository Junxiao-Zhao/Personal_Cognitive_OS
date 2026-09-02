from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from mem_core.models import Operation, proposal_hash, transaction_fingerprint
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
NOW = "2026-08-30T10:00:00+00:00"


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


def _stage_crafted_receipt(
    repository: MemoryRepository,
    operation: Operation,
    *,
    transaction_id: str,
) -> None:
    operations = [operation]
    receipt = {
        "id": transaction_id,
        "transaction_fingerprint": transaction_fingerprint(
            base_commit=repository.head(),
            profile_name=repository.profile.name,
            profile_version=repository.profile.version,
            fingerprint_context={},
            operations=operations,
        ),
        "proposal_hash": proposal_hash(operations),
        "base_commit": repository.head(),
        "profile": f"{repository.profile.name}@{repository.profile.version}",
        "fingerprint_context": {},
        "operation_count": len(operations),
        "operations": [item.normalized() for item in operations],
        "approval_receipt": None,
        "committed_at": NOW,
    }
    with (repository.root / "transactions/transactions.jsonl").open(
        "a", encoding="utf-8", newline="\n"
    ) as handle:
        handle.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n")
    repository._git("add", "transactions/transactions.jsonl")


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


def test_hook_rejects_same_length_history_rewrite_when_unstaged_profile_omits_stream(
    v05_repository: MemoryRepository, minimal_records: dict[str, dict]
) -> None:
    target = v05_repository.root / "events/events.jsonl"
    original_line = (
        json.dumps(minimal_records["events"], ensure_ascii=False, sort_keys=True) + "\n"
    )
    target.write_text(original_line, encoding="utf-8")
    v05_repository._git("add", "events/events.jsonl")
    seeded = _commit(v05_repository, "seed event", no_verify=True)
    assert seeded.returncode == 0

    tampered_line = original_line.replace('"active"', '"paused"', 1)
    assert len(tampered_line) == len(original_line)
    assert tampered_line != original_line
    target.write_text(tampered_line, encoding="utf-8")
    v05_repository._git("add", "events/events.jsonl")

    profile_path = v05_repository.root / "profiles/pco/profile.yaml"
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    profile = deepcopy(profile)
    del profile["streams"]["events"]
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

    _stage_crafted_receipt(
        v05_repository,
        Operation(
            op="write_artifact",
            path="events/events.jsonl",
            content=tampered_line,
        ),
        transaction_id="txn_profile_bypass",
    )

    result = _commit(v05_repository, "tamper with profile bypass")

    assert result.returncode != 0
    assert "APPEND_ONLY_VIOLATION" in result.stdout + result.stderr


def test_hook_rejects_write_artifact_outside_profile_artifact_roots(
    v05_repository: MemoryRepository,
) -> None:
    outside = v05_repository.root / "outside.txt"
    outside.write_text("manual artifact\n", encoding="utf-8")
    v05_repository._git("add", "outside.txt")
    _stage_crafted_receipt(
        v05_repository,
        Operation(
            op="write_artifact",
            path="outside.txt",
            content="manual artifact\n",
        ),
        transaction_id="txn_artifact_escape",
    )

    result = _commit(v05_repository, "write escaped artifact")

    assert result.returncode != 0
    assert "ARTIFACT_PATH_NOT_ALLOWED" in result.stdout + result.stderr


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
