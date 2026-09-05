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
from packaging.specifiers import SpecifierSet
from pydantic import ValidationError

from pco.proposals import paths as proposal_paths_module
from pco.proposals.models import BusinessFile, ProposalManifest
from pco.proposals.paths import ProposalPaths, active_proposals
from pco.proposals.service import ProposalService
from pco.versioning import CompatibilityContract
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


def _manifest_schema() -> dict[str, Any]:
    schema_path = files("pco.resources").joinpath(
        "schemas/proposal-manifest.schema.json"
    )
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _runtime_accepts(data: dict[str, Any]) -> bool:
    try:
        ProposalManifest.model_validate(data)
    except (ValidationError, MemError):
        return False
    return True


def _extension_uniqueness_is_valid(
    schema: dict[str, Any], data: dict[str, Any]
) -> bool:
    for collection in ("input_material", "files"):
        unique_keys = schema["properties"][collection]["x-pco-unique-by"]
        for unique_key in unique_keys:
            values = [
                item[unique_key]
                for item in data.get(collection, [])
                if unique_key in item
            ]
            if len(values) != len(set(values)):
                return False
    return True


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


@pytest.mark.parametrize("entry_kind", ["symlink", "file", "fifo", "socket"])
def test_committed_reserved_entry_must_be_a_real_directory(
    workspace: Workspace, tmp_path: Path, entry_kind: str
) -> None:
    entry = workspace.paths.proposals / ".committed"
    opened_socket: socket.socket | None = None
    if entry_kind == "symlink":
        target = tmp_path / "outside-committed"
        target.mkdir()
        entry.symlink_to(target, target_is_directory=True)
    elif entry_kind == "file":
        entry.write_text("not an archive directory", encoding="utf-8")
    elif entry_kind == "fifo":
        os.mkfifo(entry)
    else:
        opened_socket = socket.socket(socket.AF_UNIX)
        opened_socket.bind(str(entry))
    before = _snapshot_tree(workspace.paths.root)

    try:
        with pytest.raises(MemError) as exc:
            active_proposals(workspace.paths)
    finally:
        if opened_socket is not None:
            opened_socket.close()

    assert exc.value.detail.code == "PROPOSAL_PATH_UNSAFE"
    assert exc.value.detail.path == str(entry)
    assert _snapshot_tree(workspace.paths.root) == before


def test_active_scan_uses_and_closes_one_no_follow_root_descriptor(
    workspace: Workspace, tmp_path: Path, monkeypatch
) -> None:
    root = workspace.paths.proposals
    (root / "review_original").mkdir()
    replacement = tmp_path / "replacement-proposals"
    replacement.mkdir()
    (replacement / "review_attacker").mkdir()
    held_root = workspace.paths.root / "held-proposals"
    real_open = os.open
    real_close = os.close
    real_scandir = os.scandir
    opened: list[tuple[int, int]] = []
    closed: list[int] = []
    scandir_arguments: list[int | str | bytes | Path] = []
    swapped = False

    def recording_open(path: str | bytes | Path, flags: int) -> int:
        descriptor = real_open(path, flags)
        opened.append((descriptor, flags))
        return descriptor

    def recording_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    def swap_then_scan(directory: int | str | bytes | Path):
        nonlocal swapped
        scandir_arguments.append(directory)
        if not swapped:
            root.rename(held_root)
            root.symlink_to(replacement, target_is_directory=True)
            swapped = True
        return real_scandir(directory)

    monkeypatch.setattr(proposal_paths_module.os, "open", recording_open)
    monkeypatch.setattr(proposal_paths_module.os, "close", recording_close)
    monkeypatch.setattr(proposal_paths_module.os, "scandir", swap_then_scan)

    result = active_proposals(workspace.paths)

    assert result == (root / "review_original",)
    assert len(opened) == 1
    descriptor, flags = opened[0]
    assert flags & os.O_DIRECTORY
    assert flags & os.O_NOFOLLOW
    assert scandir_arguments == [descriptor]
    assert closed == [descriptor]


def test_active_scan_closes_root_descriptor_after_a_structured_child_error(
    workspace: Workspace, monkeypatch
) -> None:
    (workspace.paths.proposals / "unsafe-file").write_text("unsafe", encoding="utf-8")
    real_open = os.open
    real_close = os.close
    opened: list[int] = []
    closed: list[int] = []

    def recording_open(path: str | bytes | Path, flags: int) -> int:
        descriptor = real_open(path, flags)
        opened.append(descriptor)
        return descriptor

    def recording_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(proposal_paths_module.os, "open", recording_open)
    monkeypatch.setattr(proposal_paths_module.os, "close", recording_close)

    with pytest.raises(MemError) as exc:
        active_proposals(workspace.paths)

    assert exc.value.detail.code == "PROPOSAL_PATH_UNSAFE"
    assert len(opened) == 1
    assert closed == opened


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


@pytest.mark.parametrize(
    "schema_version",
    [
        "0.5",
        "v0.5",
        "0.5.1",
        "0.5.1.post1",
        "0.5post",
        "0.5.0_rev_1",
        "0.5.1+local",
        "0!0.5.2",
    ],
)
def test_manifest_accepts_every_compatible_pep440_version_sample(
    schema_version: str,
) -> None:
    manifest = ProposalManifest.model_validate(
        _manifest_data(schema_version=schema_version)
    )
    assert manifest.schema_version == schema_version


@pytest.mark.parametrize(
    "schema_version",
    [
        "0.4.9",
        "0.5.1rc1",
        "0.5.0.dev1",
        "0.5_1",
        " 0.5.0",
        "0.5.0\n",
        "0.6.0",
        "not-a-version",
    ],
)
def test_manifest_schema_version_failure_is_a_structured_compatibility_error(
    schema_version: str,
) -> None:
    with pytest.raises(MemError) as exc:
        ProposalManifest.model_validate(_manifest_data(schema_version=schema_version))

    assert exc.value.detail.code == "WORKSPACE_FORMAT_MISMATCH"
    assert exc.value.detail.phase == "compatibility"
    assert exc.value.detail.path == "proposal manifest"
    assert exc.value.detail.value == {
        "observed": schema_version,
        "supported": str(
            CompatibilityContract.for_package().proposal_manifest
        ),
    }


@pytest.mark.parametrize("schema_version", [None, 5, [], {}])
def test_non_string_manifest_schema_version_is_a_structured_compatibility_error(
    schema_version: Any,
) -> None:
    with pytest.raises(MemError) as exc:
        ProposalManifest.model_validate(_manifest_data(schema_version=schema_version))

    assert exc.value.detail.code == "WORKSPACE_FORMAT_MISMATCH"
    assert exc.value.detail.phase == "compatibility"
    assert exc.value.detail.path == "proposal manifest"
    assert exc.value.detail.value == {
        "observed": schema_version,
        "supported": str(
            CompatibilityContract.for_package().proposal_manifest
        ),
    }


def test_bundled_schema_enforces_the_strict_manifest_contract() -> None:
    schema = _manifest_schema()
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


def test_schema_required_fields_match_runtime_defaults() -> None:
    schema = _manifest_schema()
    validator = Draft202012Validator(schema)
    minimal = {
        "review_id": "review_defaults",
        "base_commit": "a" * 40,
        "workspace_version": "0.5.0",
        "profile_version": "0.5.0",
    }

    manifest = ProposalManifest.model_validate(minimal)

    validator.validate(minimal)
    assert set(schema["required"]) == set(minimal)
    assert schema["properties"]["schema_version"]["default"] == "0.5.0"
    assert schema["properties"]["input_material"]["default"] == []
    assert schema["properties"]["files"]["default"] == []
    assert manifest.model_dump(mode="json") == {
        **minimal,
        "schema_version": "0.5.0",
        "input_material": [],
        "files": [],
    }


@pytest.mark.parametrize(
    "schema_version",
    [
        "0.5",
        "v0.5",
        "0.5.1",
        "0.5.1.post1",
        "0.5post",
        "0.5.0_rev_1",
        "0.5.1+local",
        "0!0.5.2",
    ],
)
def test_schema_accepts_compatible_pep440_version_samples(
    schema_version: str,
) -> None:
    schema = _manifest_schema()
    schema_range = SpecifierSet(
        schema["properties"]["schema_version"]["x-pco-supported-range"]
    )
    assert str(schema_range) == str(
        CompatibilityContract.for_package().proposal_manifest
    )
    assert Draft202012Validator(schema).is_valid(
        _manifest_data(schema_version=schema_version)
    )


@pytest.mark.parametrize(
    "schema_version",
    [
        "0.4.9",
        "0.5.1rc1",
        "0.5.0.dev1",
        "0.5_1",
        " 0.5.0",
        "0.5.0\n",
        "0.6.0",
        "not-a-version",
    ],
)
def test_schema_rejects_incompatible_or_invalid_version_samples(
    schema_version: str,
) -> None:
    assert not Draft202012Validator(_manifest_schema()).is_valid(
        _manifest_data(schema_version=schema_version)
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"unexpected": True},
        {"files": [{"path": "evidence/ev_1.md", "operation": "unknown"}]},
        {
            "input_material": [
                {
                    "material_id": "artifact",
                    "kind": "proposal_artifact",
                    "path": "inputs/a.txt",
                    "locator": "file:///unexpected",
                    "fidelity": "selected_excerpt",
                }
            ]
        },
    ],
)
def test_schema_and_runtime_reject_the_same_strict_shape_errors(
    overrides: dict[str, Any],
) -> None:
    data = _manifest_data(**overrides)
    schema_valid = Draft202012Validator(_manifest_schema()).is_valid(data)

    assert schema_valid is False
    assert _runtime_accepts(data) is False


@pytest.mark.parametrize("collection", ["input_material", "files"])
def test_schema_and_runtime_reject_exact_duplicate_array_items(
    collection: str,
) -> None:
    item = (
        {
            "material_id": "artifact",
            "kind": "proposal_artifact",
            "path": "inputs/a.txt",
            "fidelity": "selected_excerpt",
        }
        if collection == "input_material"
        else {"path": "evidence/ev_1.md", "operation": "create"}
    )
    data = _manifest_data(**{collection: [item, dict(item)]})

    assert Draft202012Validator(_manifest_schema()).is_valid(data) is False
    assert _runtime_accepts(data) is False


@pytest.mark.parametrize(
    ("collection", "items", "unique_keys"),
    [
        (
            "input_material",
            [
                {
                    "material_id": "same",
                    "kind": "proposal_artifact",
                    "path": "inputs/a.txt",
                    "fidelity": "selected_excerpt",
                },
                {
                    "material_id": "same",
                    "kind": "proposal_artifact",
                    "path": "inputs/b.txt",
                    "fidelity": "selected_excerpt",
                },
            ],
            ["material_id", "path"],
        ),
        (
            "input_material",
            [
                {
                    "material_id": "first",
                    "kind": "proposal_artifact",
                    "path": "inputs/same.txt",
                    "fidelity": "selected_excerpt",
                },
                {
                    "material_id": "second",
                    "kind": "proposal_artifact",
                    "path": "inputs/same.txt",
                    "fidelity": "selected_excerpt",
                },
            ],
            ["material_id", "path"],
        ),
        (
            "files",
            [
                {"path": "evidence/ev_1.md", "operation": "create"},
                {"path": "evidence/ev_1.md", "operation": "revise"},
            ],
            ["path"],
        ),
    ],
)
def test_schema_documents_property_uniqueness_as_an_explicit_extension(
    collection: str,
    items: list[dict[str, Any]],
    unique_keys: list[str],
) -> None:
    schema = _manifest_schema()
    data = _manifest_data(**{collection: items})

    # Standard uniqueItems compares whole objects and cannot enforce one
    # property's uniqueness. Generic validators therefore accept this shape.
    assert Draft202012Validator(schema).is_valid(data) is True
    assert schema["properties"][collection]["x-pco-unique-by"] == unique_keys
    assert _extension_uniqueness_is_valid(schema, data) is False
    assert _runtime_accepts(data) is False
