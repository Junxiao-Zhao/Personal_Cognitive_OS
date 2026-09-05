from __future__ import annotations

import hashlib
from importlib.resources import files
import json
import os
import socket
import stat
from pathlib import Path
from typing import Any

import pytest
from mem_core.errors import MemError
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from pco.proposals.models import BusinessFile, ProposalManifest
from pco.proposals.paths import ProposalPaths, active_proposals
from pco.proposals.service import ProposalService
from pco.workspace import Workspace


def _snapshot_tree(root: Path) -> dict[str, tuple[Any, ...]]:
    """Capture a tree without following symlinks for read-only assertions."""
    root_info = root.lstat()
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


def _manifest_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": "0.5.0",
        "review_id": "review_contract",
        "base_commit": "a" * 40,
        "workspace_version": "0.5.0",
        "profile_version": "0.5.0",
        "input_material": [],
        "files": [],
    }
    data.update(overrides)
    return data


def test_scaffold_is_read_only_and_uses_current_controls(
    workspace: Workspace,
) -> None:
    before = _snapshot_tree(workspace.paths.root)
    before_tmp = _snapshot_tree(workspace.paths.tmp)

    result = ProposalService(workspace).scaffold()

    assert result["ok"] is True
    assert result["manifest"] == {
        "schema_version": "0.5.0",
        "review_id": result["manifest"]["review_id"],
        "base_commit": workspace.repository.head(),
        "workspace_version": "0.5.0",
        "profile_version": "0.5.0",
        "input_material": [],
        "files": [],
    }
    assert result["manifest"]["review_id"].startswith("review_")
    assert not (workspace.paths.proposals / result["manifest"]["review_id"]).exists()
    assert _snapshot_tree(workspace.paths.root) == before
    assert _snapshot_tree(workspace.paths.tmp) == before_tmp


def test_status_and_service_construction_leave_the_workspace_unchanged(
    workspace: Workspace,
) -> None:
    before = _snapshot_tree(workspace.paths.root)
    before_tmp = _snapshot_tree(workspace.paths.tmp)

    service = ProposalService(workspace)
    result = service.status()

    assert result == {"ok": True, "active_proposal": None}
    assert _snapshot_tree(workspace.paths.root) == before
    assert _snapshot_tree(workspace.paths.tmp) == before_tmp


def test_scaffold_does_not_reserve_its_generated_review_id(
    workspace: Workspace,
) -> None:
    before = _snapshot_tree(workspace.paths.root)

    first = ProposalService(workspace).scaffold()
    second = ProposalService(workspace).scaffold()

    assert first["manifest"]["review_id"] != second["manifest"]["review_id"]
    assert _snapshot_tree(workspace.paths.root) == before


def test_scaffold_rejects_existing_active_proposal(
    workspace: Workspace, proposal_factory
) -> None:
    proposal_factory("review_active")
    before = _snapshot_tree(workspace.paths.root)

    with pytest.raises(MemError) as exc:
        ProposalService(workspace).scaffold()

    assert exc.value.detail.code == "PROPOSAL_ALREADY_ACTIVE"
    assert _snapshot_tree(workspace.paths.root) == before


def test_status_reports_the_only_immediate_active_proposal(
    workspace: Workspace, proposal_factory
) -> None:
    root = proposal_factory("review_active")
    (root / "nested").mkdir()
    (root / "nested" / "ignored.txt").write_text("content", encoding="utf-8")

    assert ProposalService(workspace).status() == {
        "ok": True,
        "active_proposal": {
            "review_id": "review_active",
            "path": str(root),
        },
    }


def test_committed_directory_is_not_an_active_proposal(workspace: Workspace) -> None:
    committed = workspace.paths.proposals / ".committed" / "commit_example"
    committed.mkdir(parents=True)

    assert active_proposals(workspace.paths) == ()
    assert ProposalService(workspace).status()["active_proposal"] is None


def test_multiple_active_roots_have_no_selected_winner(
    workspace: Workspace, proposal_factory
) -> None:
    proposal_factory("review_first")
    proposal_factory("review_second")

    with pytest.raises(MemError) as exc:
        ProposalService(workspace).status()

    assert exc.value.detail.code == "PROPOSAL_MANIFEST_MISMATCH"
    assert exc.value.detail.value == ["review_first", "review_second"]


@pytest.mark.parametrize("entry_kind", ["symlink", "file", "fifo", "socket"])
def test_active_scan_rejects_unsafe_or_special_immediate_entries(
    workspace: Workspace, tmp_path: Path, entry_kind: str
) -> None:
    entry = workspace.paths.proposals / "review_unsafe"
    opened_socket: socket.socket | None = None
    if entry_kind == "symlink":
        target = tmp_path / "outside"
        target.mkdir()
        entry.symlink_to(target, target_is_directory=True)
    elif entry_kind == "file":
        entry.write_text("not a proposal directory", encoding="utf-8")
    elif entry_kind == "fifo":
        os.mkfifo(entry)
    else:
        opened_socket = socket.socket(socket.AF_UNIX)
        opened_socket.bind(str(entry))

    try:
        with pytest.raises(MemError) as exc:
            ProposalPaths(workspace.paths).active_proposals()
    finally:
        if opened_socket is not None:
            opened_socket.close()

    assert exc.value.detail.code == "PROPOSAL_PATH_UNSAFE"
    assert exc.value.detail.path == str(entry)


def test_active_scan_does_not_descend_into_a_proposal(
    workspace: Workspace, proposal_factory, tmp_path: Path
) -> None:
    proposal = proposal_factory("review_active")
    (proposal / "nested_link").symlink_to(tmp_path, target_is_directory=True)

    assert active_proposals(workspace.paths) == (proposal,)


def test_manifest_models_are_strict_and_preserve_the_two_material_shapes() -> None:
    manifest = ProposalManifest.model_validate(
        _manifest_data(
            schema_version="0.5.1",
            input_material=[
                {
                    "material_id": "journal_current",
                    "kind": "local_file",
                    "locator": "file:///journal.md",
                    "expected_content_hash": "sha256:" + "b" * 64,
                    "fidelity": "full_source",
                },
                {
                    "material_id": "remote_excerpt_1",
                    "kind": "proposal_artifact",
                    "path": "inputs/source_1.txt",
                    "expected_content_hash": None,
                    "fidelity": "selected_excerpt",
                },
            ],
            files=[{"path": "hypotheses/hyp_1.md", "operation": "create"}],
        )
    )

    dumped = manifest.model_dump(mode="json")
    assert dumped["schema_version"] == "0.5.1"
    assert dumped["input_material"][0]["locator"] == "file:///journal.md"
    assert dumped["input_material"][1]["path"] == "inputs/source_1.txt"
    assert dumped["files"] == [
        {"path": "hypotheses/hyp_1.md", "operation": "create"}
    ]

    with pytest.raises(ValidationError):
        ProposalManifest.model_validate(_manifest_data(unexpected=True))
    with pytest.raises(ValidationError):
        BusinessFile.model_validate(
            {"path": "hypotheses/hyp_1.md", "operation": "create", "extra": 1}
        )


def test_manifest_does_not_coerce_container_types() -> None:
    with pytest.raises(ValidationError):
        ProposalManifest.model_validate(_manifest_data(files=()))


@pytest.mark.parametrize(
    "path",
    ["", ".", "/absolute.md", "../escape.md", "inputs/../escape.md", "./x.md", "a//b.md", "a\\b.md"],
)
def test_manifest_rejects_noncanonical_or_unsafe_proposal_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        BusinessFile.model_validate({"path": path, "operation": "create"})

    with pytest.raises(ValidationError):
        ProposalManifest.model_validate(
            _manifest_data(
                input_material=[
                    {
                        "material_id": "artifact",
                        "kind": "proposal_artifact",
                        "path": path,
                        "fidelity": "selected_excerpt",
                    }
                ]
            )
        )


@pytest.mark.parametrize("schema_version", ["0.4.9", "0.6.0", "not-a-version"])
def test_manifest_schema_version_must_be_in_the_package_contract(
    schema_version: str,
) -> None:
    with pytest.raises((ValidationError, MemError)):
        ProposalManifest.model_validate(_manifest_data(schema_version=schema_version))


def test_bundled_schema_enforces_the_strict_manifest_contract() -> None:
    schema_path = files("pco.resources").joinpath(
        "schemas/proposal-manifest.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    valid = ProposalManifest.model_validate(
        _manifest_data(
            schema_version="0.5.1",
            files=[{"path": "evidence/ev_1.md", "operation": "create"}],
        )
    ).model_dump(mode="json")

    validator.validate(valid)
    invalid = dict(valid, unexpected=True)
    assert not validator.is_valid(invalid)
    incompatible = dict(valid, schema_version="0.6.0")
    assert not validator.is_valid(incompatible)
