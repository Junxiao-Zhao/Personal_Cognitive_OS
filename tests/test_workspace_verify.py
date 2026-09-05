from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

import pytest

from mem_core.errors import MemError
from pco.verify import verify_workspace
from pco.workspace import Workspace


def _snapshot_tree(root: Path) -> dict[str, tuple[Any, ...]]:
    """Capture every workspace entry without following symlinks."""
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"snapshot root must be a real directory: {root}")
    snapshot: dict[str, tuple[Any, ...]] = {
        ".": (
            stat.S_IFMT(root_info.st_mode),
            stat.S_IMODE(root_info.st_mode),
            root_info.st_size,
            root_info.st_mtime_ns,
            root_info.st_ctime_ns,
        )
    }
    pending = [(root, Path("."))]
    while pending:
        directory, prefix = pending.pop()
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            relative = (prefix / entry.name).as_posix()
            entry_path = Path(entry.path)
            info = entry.stat(follow_symlinks=False)
            metadata = (
                stat.S_IFMT(info.st_mode),
                stat.S_IMODE(info.st_mode),
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
            if stat.S_ISREG(info.st_mode):
                snapshot[relative] = (
                    *metadata,
                    hashlib.sha256(entry_path.read_bytes()).hexdigest(),
                )
            elif stat.S_ISLNK(info.st_mode):
                snapshot[relative] = (*metadata, os.readlink(entry.path))
            else:
                snapshot[relative] = metadata
            if stat.S_ISDIR(info.st_mode):
                pending.append((entry_path, prefix / entry.name))
    return snapshot


def _commit_unverified_transaction(
    workspace: Workspace, transaction: dict[str, Any]
) -> None:
    transaction_log = workspace.paths.canonical / "transactions/transactions.jsonl"
    with transaction_log.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(transaction, sort_keys=True) + "\n")
    workspace.repository._git("add", "transactions/transactions.jsonl")
    workspace.repository._git("commit", "--no-verify", "-m", "test receipt")


def _amend_root_identity(workspace: Workspace, identity: dict[str, Any]) -> None:
    metadata = workspace.paths.canonical / "workspace.json"
    metadata.write_text(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    workspace.repository._git("add", "workspace.json")
    workspace.repository._git("commit", "--amend", "--no-verify", "--no-edit")


def test_doctor_reports_identity_compatibility_skills_and_permission_contract(
    workspace: Workspace,
) -> None:
    """Removing a verification surface would hide an integrity boundary."""
    result = workspace.doctor()

    assert result["ok"] is True
    assert result["identity"] == workspace.identity
    assert result["compatibility"]["ok"] is True
    assert result["skills"] == {
        "pco-memory": {"ok": True, "name": "pco-memory", "version": "0.5.0"},
        "pco-consolidate": {
            "ok": True,
            "name": "pco-consolidate",
            "version": "0.5.0",
        },
    }
    assert result["permission_contract"] == {
        "read_commands_need_write_permission": False,
        "proposal_authoring": "one-time Host Edit permission",
        "canonical_submission": "separate one-time Host commit permission",
        "permanent_workspace_write_permission": "unsupported",
        "tui_discovery_or_approval_proven": False,
    }


def test_doctor_rejects_changed_workspace_id_read_only(workspace: Workspace) -> None:
    """Accepting a replacement ID would let a copied identity be substituted."""
    metadata = workspace.paths.canonical / "workspace.json"
    metadata.write_text(
        '{"workspace_id":"workspace_replaced","workspace_format_version":"0.5.0","profile_version":"0.5.0"}\n',
        encoding="utf-8",
    )
    before = _snapshot_tree(workspace.paths.root)

    with pytest.raises(MemError) as exc:
        workspace.doctor()

    assert exc.value.detail.code == "WORKSPACE_INTEGRITY_ERROR"
    assert exc.value.detail.phase == "doctor"
    assert exc.value.detail.canonical_changed is False
    assert exc.value.detail.retry_safe is False
    assert _snapshot_tree(workspace.paths.root) == before


def test_doctor_rejects_workspace_metadata_not_exactly_from_root_commit(
    workspace: Workspace,
) -> None:
    """Ignoring byte-level metadata rewrites would weaken the root anchor."""
    metadata = workspace.paths.canonical / "workspace.json"
    metadata.write_text(metadata.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")

    with pytest.raises(MemError) as exc:
        workspace.doctor()

    assert exc.value.detail.code == "WORKSPACE_INTEGRITY_ERROR"


@pytest.mark.parametrize(
    "replacement",
    [
        {"workspace_id": "workspace_bad", "workspace_format_version": "0.5.0", "profile_version": "0.5.0"},
        {"workspace_id": "workspace_" + "a" * 32, "workspace_format_version": "0.6.0", "profile_version": "0.5.0"},
        {"workspace_id": "workspace_" + "a" * 32, "workspace_format_version": "0.5.0", "profile_version": "0.5.0", "unexpected": True},
    ],
    ids=["invalid-id", "unsupported-version", "unknown-field"],
)
def test_doctor_rejects_invalid_workspace_metadata_schema(
    workspace: Workspace, replacement: dict[str, Any]
) -> None:
    """Permissive identity parsing would make workspace compatibility ambiguous."""
    (workspace.paths.canonical / "workspace.json").write_text(
        json.dumps(replacement) + "\n", encoding="utf-8"
    )

    with pytest.raises(MemError) as exc:
        workspace.doctor()

    assert exc.value.detail.code == "WORKSPACE_INTEGRITY_ERROR"


def test_doctor_rejects_transaction_receipt_for_another_workspace(
    workspace: Workspace,
) -> None:
    """Accepting a foreign receipt would merge audit evidence across workspaces."""
    _commit_unverified_transaction(
        workspace,
        {
            "id": "txn_foreign_receipt",
            "receipt": {"workspace_id": "workspace_other", "receipt_id": "commit_x"},
        },
    )
    before = _snapshot_tree(workspace.paths.root)

    with pytest.raises(MemError) as exc:
        workspace.doctor()

    assert exc.value.detail.code == "WORKSPACE_INTEGRITY_ERROR"
    assert exc.value.detail.phase == "doctor"
    assert _snapshot_tree(workspace.paths.root) == before


def test_doctor_keeps_transaction_parse_line_diagnostics(workspace: Workspace) -> None:
    """A malformed receipt log must identify its own canonical line, not metadata."""
    transaction_log = workspace.paths.canonical / "transactions/transactions.jsonl"
    transaction_log.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(MemError) as exc:
        workspace.doctor()

    assert exc.value.detail.code == "WORKSPACE_INTEGRITY_ERROR"
    assert exc.value.detail.path == "transactions/transactions.jsonl:1"


def test_doctor_accepts_transaction_receipt_for_this_workspace(workspace: Workspace) -> None:
    """Valid workspace-scoped receipts remain verifiable canonical history."""
    _commit_unverified_transaction(
        workspace,
        {
            "id": "txn_local_receipt",
            "receipt": {
                "workspace_id": workspace.identity["workspace_id"],
                "receipt_id": "commit_x",
            },
        },
    )

    assert workspace.doctor()["identity"] == workspace.identity


def test_doctor_checks_skill_compatibility_but_verify_rejects_payload_tampering_read_only(
    workspace: Workspace,
) -> None:
    """Doctor must not hash payloads, while verify must reject altered instructions."""
    skill = workspace.paths.skills / "pco-memory" / "SKILL.md"
    skill.write_text("tampered\n", encoding="utf-8")
    before = _snapshot_tree(workspace.paths.root)

    assert workspace.doctor()["skills"]["pco-memory"]["name"] == "pco-memory"
    assert _snapshot_tree(workspace.paths.root) == before

    with pytest.raises(MemError) as exc:
        verify_workspace(workspace)

    assert exc.value.detail.code == "WORKSPACE_SKILL_INVALID"
    assert _snapshot_tree(workspace.paths.root) == before


def test_verify_workspace_is_read_only_on_success(workspace: Workspace) -> None:
    """Verification must not create Git, lock, state, or temporary artifacts."""
    before = _snapshot_tree(workspace.paths.root)

    result = verify_workspace(workspace)

    assert result["ok"] is True
    assert _snapshot_tree(workspace.paths.root) == before


def test_projection_verification_is_explicitly_deferred_read_only(
    workspace: Workspace,
) -> None:
    """Claiming an empty legacy projection is verified would be misleading."""
    before = _snapshot_tree(workspace.paths.root)

    with pytest.raises(MemError) as exc:
        verify_workspace(workspace, path=workspace.paths.projection)

    assert exc.value.detail.code == "WORKSPACE_VERIFICATION_DEFERRED"
    assert exc.value.detail.canonical_changed is False
    assert _snapshot_tree(workspace.paths.root) == before


def test_copying_complete_canonical_repo_preserves_workspace_id(
    workspace: Workspace, tmp_path: Path
) -> None:
    """A complete canonical restoration retains, rather than regenerates, identity."""
    restored_root = tmp_path / "restored"
    (restored_root / ".pco").mkdir(parents=True)
    shutil.copytree(workspace.paths.canonical, restored_root / ".pco/canonical")

    restored = Workspace.open(restored_root)

    assert restored.identity["workspace_id"] == workspace.identity["workspace_id"]


def test_snapshot_tree_rejects_symlink_or_non_directory_root(tmp_path: Path) -> None:
    """A snapshot that traverses a link could miss verification side effects."""
    directory = tmp_path / "directory"
    directory.mkdir()
    link = tmp_path / "link"
    link.symlink_to(directory, target_is_directory=True)
    file = tmp_path / "file"
    file.write_text("not a directory\n", encoding="utf-8")

    for root in (link, file):
        with pytest.raises(ValueError, match="real directory"):
            _snapshot_tree(root)


def test_doctor_uses_working_identity_bytes_without_git_clean_filter(
    workspace: Workspace, tmp_path: Path
) -> None:
    """A Git clean filter must neither run nor redefine exact identity bytes."""
    marker = tmp_path / "filter-ran"
    cleaner = tmp_path / "cleaner"
    cleaner.write_text(f"#!/bin/sh\nprintf invoked > {marker}\ncat\n", encoding="utf-8")
    cleaner.chmod(0o755)
    workspace.repository._git("config", "filter.workspace-identity.clean", str(cleaner))
    (workspace.paths.canonical / ".gitattributes").write_text(
        "workspace.json filter=workspace-identity\n", encoding="utf-8"
    )
    before = _snapshot_tree(workspace.paths.root)

    assert workspace.doctor()["ok"] is True

    assert not marker.exists()
    assert _snapshot_tree(workspace.paths.root) == before


def test_doctor_rejects_eol_normalized_workspace_bytes_read_only(
    workspace: Workspace,
) -> None:
    """EOL filtering cannot make different working identity bytes appear anchored."""
    metadata = workspace.paths.canonical / "workspace.json"
    (workspace.paths.canonical / ".gitattributes").write_text(
        "workspace.json text\n", encoding="utf-8"
    )
    metadata.write_bytes(metadata.read_bytes().replace(b"\n", b"\r\n"))
    before = _snapshot_tree(workspace.paths.root)

    with pytest.raises(MemError) as exc:
        workspace.doctor()

    assert exc.value.detail.code == "WORKSPACE_INTEGRITY_ERROR"
    assert _snapshot_tree(workspace.paths.root) == before


def test_doctor_rejects_shallow_canonical_repository_read_only(workspace: Workspace) -> None:
    """A shallow boundary cannot substitute for the immutable initial commit."""
    root_commit = workspace.repository._git("rev-list", "--max-parents=0", "HEAD")
    (workspace.paths.canonical / ".git/shallow").write_text(
        root_commit + "\n", encoding="utf-8"
    )
    before = _snapshot_tree(workspace.paths.root)

    with pytest.raises(MemError) as exc:
        workspace.doctor()

    assert exc.value.detail.code == "WORKSPACE_INTEGRITY_ERROR"
    assert "shallow canonical repository" in exc.value.detail.message
    assert _snapshot_tree(workspace.paths.root) == before


def test_doctor_rejects_multiple_reachable_root_commits_read_only(
    workspace: Workspace,
) -> None:
    """Choosing one of two roots would allow identity-anchor ambiguity."""
    head = workspace.repository.head()
    tree = workspace.repository._git("rev-parse", f"{head}^{{tree}}")
    unrelated = workspace.repository._git("commit-tree", tree, "-m", "unrelated root")
    merged = workspace.repository._git(
        "commit-tree", tree, "-p", head, "-p", unrelated, "-m", "merge roots"
    )
    workspace.repository._git("update-ref", "HEAD", merged)
    before = _snapshot_tree(workspace.paths.root)

    with pytest.raises(MemError) as exc:
        workspace.doctor()

    assert exc.value.detail.code == "WORKSPACE_INTEGRITY_ERROR"
    assert "exactly one reachable root" in exc.value.detail.message
    assert _snapshot_tree(workspace.paths.root) == before


def test_doctor_accepts_root_anchored_extra_fields_and_compatible_versions(
    workspace: Workspace,
) -> None:
    """Schema extensions and v0.5 patches remain valid when rooted identically."""
    identity = {
        **workspace.identity,
        "workspace_format_version": "0.5.1",
        "profile_version": "0.5.1",
        "root_anchored_extension": {"preserve": True},
    }
    _amend_root_identity(workspace, identity)

    result = workspace.doctor()

    assert result["identity"] == identity
    assert result["compatibility"]["workspace"] == "0.5.1"
    assert result["compatibility"]["profile"] == "0.5.1"


@pytest.mark.parametrize("field", ["workspace_format_version", "profile_version"])
def test_doctor_reports_out_of_range_root_anchored_versions_as_format_mismatch(
    workspace: Workspace, field: str
) -> None:
    """Compatibility failures must not be disguised as identity tampering."""
    identity = {**workspace.identity, field: "0.6.0"}
    _amend_root_identity(workspace, identity)
    before = _snapshot_tree(workspace.paths.root)

    with pytest.raises(MemError) as exc:
        workspace.doctor()

    assert exc.value.detail.code == "WORKSPACE_FORMAT_MISMATCH"
    assert _snapshot_tree(workspace.paths.root) == before


@pytest.mark.parametrize("ancestor", [".agents", ".agents/skills"])
@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_doctor_rejects_symlinked_skill_ancestors_without_following_outside_workspace(
    workspace: Workspace, tmp_path: Path, ancestor: str, kind: str
) -> None:
    """A trusted external SKILL tree must not satisfy a workspace-local check."""
    target = workspace.paths.root / ancestor
    parked = workspace.paths.root / f"{target.name}-parked"
    outside = tmp_path / f"outside-{target.name}"
    target.rename(parked)
    if kind == "symlink":
        shutil.copytree(parked, outside)
        target.symlink_to(outside, target_is_directory=True)
    else:
        target.write_text("not a directory\n", encoding="utf-8")
    before = _snapshot_tree(workspace.paths.root)
    outside_before = _snapshot_tree(outside) if kind == "symlink" else None

    with pytest.raises(MemError) as exc:
        workspace.doctor()

    assert exc.value.detail.code == "WORKSPACE_SKILL_INVALID"
    assert "workspace SKILL ancestor must be a real directory" in exc.value.detail.message
    assert _snapshot_tree(workspace.paths.root) == before
    if kind == "symlink":
        assert outside_before is not None
        assert _snapshot_tree(outside) == outside_before
