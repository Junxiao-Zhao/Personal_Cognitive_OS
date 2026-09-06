from __future__ import annotations

import json
import errno
import os
from pathlib import PurePosixPath

from mem_core.errors import MemError
import pytest

from pco.proposals.models import BusinessFile, ProposalArtifactMaterial, ProposalManifest
from pco.proposals import snapshot as snapshot_module
from pco.proposals.snapshot import capture_snapshot


def _manifest(root, workspace, *, files=(), material=()):
    value = ProposalManifest(
        review_id=root.name,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        files=list(files),
        input_material=list(material),
    )
    (root / "manifest.json").write_text(
        json.dumps(value.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return value


def test_snapshot_captures_exact_closed_tree_in_utf8_path_order(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    root = proposal_factory("review_snapshot")
    for directory in ("evidence", "hypotheses", "inputs"):
        (root / directory).mkdir()
    (root / "evidence/ev_z.md").write_bytes(
        business_markdown_factory(
            object_type="evidence",
            record_id="ev_z",
            fields={
                "change_summary": "create",
                "speaker": "user",
                "source_kind": "conversation",
                "user_authored": True,
                "fidelity": "selected_excerpt",
                "status": "active",
            },
        )
    )
    (root / "inputs/source.txt").write_bytes(b"source\r\n")
    manifest = _manifest(
        root,
        workspace,
        files=[BusinessFile(path="evidence/ev_z.md", operation="create")],
        material=[
            ProposalArtifactMaterial(
                material_id="source",
                kind="proposal_artifact",
                path="inputs/source.txt",
                fidelity="selected_excerpt",
            )
        ],
    )

    snapshot = capture_snapshot(root, manifest, include_local=False)

    assert snapshot.manifest_bytes == (root / "manifest.json").read_bytes()
    assert [item.path for item in snapshot.business_files] == [
        PurePosixPath("evidence/ev_z.md")
    ]
    assert snapshot.input_material[0].normalized_bytes == b"source\n"
    assert [entry.path.as_posix() for entry in snapshot.tree_entries] == sorted(
        [entry.path.as_posix() for entry in snapshot.tree_entries],
        key=lambda value: value.encode("utf-8"),
    )
    first = snapshot.manifest
    first.files.clear()
    assert len(snapshot.manifest.files) == 1


@pytest.mark.parametrize("kind", ["unknown", "unlisted_input", "symlink", "nested", "missing"])
def test_snapshot_rejects_closed_set_mismatch(
    workspace, proposal_factory, business_markdown_factory, tmp_path, kind
) -> None:
    root = proposal_factory(f"review_{kind}")
    (root / "evidence").mkdir()
    path = root / "evidence/ev_one.md"
    path.write_bytes(
        business_markdown_factory(
            object_type="evidence",
            record_id="ev_one",
            fields={
                "change_summary": "create",
                "speaker": "user",
                "source_kind": "conversation",
                "user_authored": True,
                "fidelity": "selected_excerpt",
                "status": "active",
            },
        )
    )
    manifest = _manifest(
        root,
        workspace,
        files=[BusinessFile(path="evidence/ev_one.md", operation="create")],
    )
    if kind == "unknown":
        (root / "unknown.txt").write_text("x", encoding="utf-8")
    elif kind == "unlisted_input":
        (root / "inputs").mkdir()
        (root / "inputs/nope.txt").write_text("x", encoding="utf-8")
    elif kind == "symlink":
        (root / "link").symlink_to(tmp_path / "elsewhere")
    elif kind == "nested":
        (root / "evidence/nested").mkdir()
    else:
        path.unlink()

    with pytest.raises(MemError) as exc:
        capture_snapshot(root, manifest, include_local=False)

    assert exc.value.detail.code in {
        "PROPOSAL_PATH_UNSAFE",
        "PROPOSAL_MANIFEST_MISMATCH",
    }


def test_snapshot_budget_is_exact_and_checked_incrementally(
    workspace, proposal_factory, monkeypatch
) -> None:
    root = proposal_factory("review_budget")
    manifest = _manifest(root, workspace)
    exact = len((root / "manifest.json").read_bytes())
    monkeypatch.setattr(snapshot_module, "MAX_IMMUTABLE_SNAPSHOT_BYTES", exact)
    assert capture_snapshot(root, manifest, include_local=False).total_bytes == exact
    monkeypatch.setattr(snapshot_module, "MAX_IMMUTABLE_SNAPSHOT_BYTES", exact - 1)
    with pytest.raises(MemError) as exc:
        capture_snapshot(root, manifest, include_local=False)
    assert exc.value.detail.code == "PROPOSAL_SNAPSHOT_TOO_LARGE"


def test_snapshot_budget_rejects_the_first_business_byte_over_remaining_space(
    workspace, proposal_factory, monkeypatch
) -> None:
    root = proposal_factory("review_business_budget")
    (root / "evidence").mkdir()
    (root / "evidence/ev_one.md").write_bytes(b"abcd")
    manifest = _manifest(
        root,
        workspace,
        files=[BusinessFile(path="evidence/ev_one.md", operation="create")],
    )
    manifest_size = len((root / "manifest.json").read_bytes())
    monkeypatch.setattr(
        snapshot_module, "MAX_IMMUTABLE_SNAPSHOT_BYTES", manifest_size + 3
    )
    with pytest.raises(MemError) as exc:
        capture_snapshot(root, manifest, include_local=False)
    assert exc.value.detail.code == "PROPOSAL_SNAPSHOT_TOO_LARGE"


def test_manifest_duplicate_json_key_is_not_silently_collapsed(
    workspace, proposal_factory
) -> None:
    root = proposal_factory("review_duplicate_json")
    manifest = _manifest(root, workspace)
    data = (root / "manifest.json").read_text(encoding="utf-8")
    (root / "manifest.json").write_text(
        data.replace("{", '{"review_id":"review_attacker",', 1),
        encoding="utf-8",
    )
    with pytest.raises(MemError) as exc:
        capture_snapshot(root, manifest, include_local=False)
    assert exc.value.detail.code == "PROPOSAL_MANIFEST_MISMATCH"


def test_snapshot_detects_tree_change_after_a_listed_read(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    root = proposal_factory("review_change")
    (root / "evidence").mkdir()
    target = root / "evidence/ev_one.md"
    target.write_bytes(
        business_markdown_factory(
            object_type="evidence",
            record_id="ev_one",
            fields={
                "change_summary": "create",
                "speaker": "user",
                "source_kind": "conversation",
                "user_authored": True,
                "fidelity": "selected_excerpt",
                "status": "active",
            },
        )
    )
    manifest = _manifest(
        root,
        workspace,
        files=[BusinessFile(path="evidence/ev_one.md", operation="create")],
    )
    real_read = os.read
    changed = False
    nonempty_reads = 0

    def mutate_after_read(fd: int, size: int) -> bytes:
        nonlocal changed, nonempty_reads
        result = real_read(fd, size)
        if result:
            nonempty_reads += 1
        if result and nonempty_reads == 2 and not changed:
            changed = True
            (root / "evidence/added.md").write_text("added", encoding="utf-8")
        return result

    monkeypatch.setattr(snapshot_module.os, "read", mutate_after_read)
    with pytest.raises(MemError) as exc:
        capture_snapshot(root, manifest, include_local=False)
    assert exc.value.detail.code == "PROPOSAL_CHANGED_DURING_SNAPSHOT"


def test_snapshot_rejects_review_root_and_exact_manifest_disagreement(
    workspace, proposal_factory
) -> None:
    root = proposal_factory("review_right")
    manifest = _manifest(root, workspace)
    wrong = manifest.model_copy(update={"review_id": "review_wrong"})
    with pytest.raises(MemError) as exc:
        capture_snapshot(root, wrong, include_local=False)
    assert exc.value.detail.code == "PROPOSAL_MANIFEST_MISMATCH"


def test_snapshot_binds_the_read_manifest_identity_to_the_first_tree_enumeration(
    workspace, proposal_factory, monkeypatch
) -> None:
    root = proposal_factory("review_manifest_swap")
    manifest = _manifest(root, workspace)
    original = (root / "manifest.json").read_bytes()
    replacement = original.replace(b'"files": []', b'"files": [] ')
    real_enumerate = snapshot_module._enumerate_tree
    calls = 0

    def replace_before_first_enumeration(root_fd: int, proposal_root):
        nonlocal calls
        calls += 1
        if calls == 1:
            (root / "manifest.json").write_bytes(replacement)
        return real_enumerate(root_fd, proposal_root)

    monkeypatch.setattr(snapshot_module, "_enumerate_tree", replace_before_first_enumeration)

    with pytest.raises(MemError) as exc:
        capture_snapshot(root, manifest, include_local=False)

    assert exc.value.detail.code == "PROPOSAL_CHANGED_DURING_SNAPSHOT"


def test_snapshot_maps_root_fstat_failure_and_closes_the_root_descriptor(
    workspace, proposal_factory, monkeypatch
) -> None:
    root = proposal_factory("review_root_fstat")
    manifest = _manifest(root, workspace)
    real_open = os.open
    real_close = os.close
    opened: list[int] = []
    closed: list[int] = []

    def recording_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def fail_fstat(_descriptor: int):
        raise OSError(errno.EIO, "injected root fstat failure")

    def recording_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(snapshot_module.os, "open", recording_open)
    monkeypatch.setattr(
        snapshot_module.os,
        "supports_dir_fd",
        set(os.supports_dir_fd) | {recording_open},
    )
    monkeypatch.setattr(snapshot_module.os, "fstat", fail_fstat)
    monkeypatch.setattr(snapshot_module.os, "close", recording_close)

    with pytest.raises(MemError) as exc:
        capture_snapshot(root, manifest, include_local=False)

    assert exc.value.detail.code == "PROPOSAL_PATH_UNSAFE"
    assert "injected root fstat failure" in exc.value.detail.value
    assert closed == list(reversed(opened))


def test_snapshot_maps_nested_directory_fstat_failure_and_closes_every_descriptor(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    root = proposal_factory("review_directory_fstat")
    (root / "evidence").mkdir()
    (root / "evidence/ev_one.md").write_bytes(
        business_markdown_factory(
            object_type="evidence",
            record_id="ev_one",
            fields={
                "change_summary": "create",
                "speaker": "user",
                "source_kind": "conversation",
                "user_authored": True,
                "fidelity": "selected_excerpt",
                "status": "active",
            },
        )
    )
    manifest = _manifest(
        root,
        workspace,
        files=[BusinessFile(path="evidence/ev_one.md", operation="create")],
    )
    real_fstat = os.fstat
    calls = 0

    directory_fstats = 0

    def fail_directory_fstat(descriptor: int):
        nonlocal calls
        nonlocal directory_fstats
        calls += 1
        observed = real_fstat(descriptor)
        if os.path.isdir(f"/proc/self/fd/{descriptor}"):
            directory_fstats += 1
        if directory_fstats == 2:
            raise OSError(errno.EIO, "injected directory fstat failure")
        return observed

    monkeypatch.setattr(snapshot_module.os, "fstat", fail_directory_fstat)
    before = len(os.listdir("/proc/self/fd"))

    with pytest.raises(MemError) as exc:
        capture_snapshot(root, manifest, include_local=False)

    assert exc.value.detail.code == "PROPOSAL_PATH_UNSAFE"
    assert "injected directory fstat failure" in exc.value.detail.value
    assert len(os.listdir("/proc/self/fd")) == before


def test_snapshot_rejects_a_filesystem_name_that_is_not_utf8(
    workspace, proposal_factory
) -> None:
    root = proposal_factory("review_non_utf8_name")
    manifest = _manifest(root, workspace)
    root_bytes = os.fsencode(root)
    bad_name = root_bytes + b"/bad-\xff"
    descriptor = os.open(bad_name, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(descriptor)

    with pytest.raises(MemError) as exc:
        capture_snapshot(root, manifest, include_local=False)

    assert exc.value.detail.code == "PROPOSAL_PATH_UNSAFE"


def test_snapshot_maps_scandir_failure_and_closes_the_root_descriptor(
    workspace, proposal_factory, monkeypatch
) -> None:
    root = proposal_factory("review_scandir_failure")
    manifest = _manifest(root, workspace)
    real_open = os.open
    real_close = os.close
    opened: list[int] = []
    closed: list[int] = []

    def recording_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def recording_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    def fail_scandir(_descriptor: int):
        raise OSError(errno.EIO, "injected scandir failure")

    monkeypatch.setattr(snapshot_module.os, "open", recording_open)
    monkeypatch.setattr(snapshot_module.os, "close", recording_close)
    monkeypatch.setattr(snapshot_module.os, "scandir", fail_scandir)
    monkeypatch.setattr(
        snapshot_module.os,
        "supports_dir_fd",
        set(os.supports_dir_fd) | {recording_open},
    )

    with pytest.raises(MemError) as exc:
        capture_snapshot(root, manifest, include_local=False)

    assert exc.value.detail.code == "PROPOSAL_PATH_UNSAFE"
    assert "injected scandir failure" in exc.value.detail.value
    assert closed == list(reversed(opened))


def test_snapshot_rejects_named_root_swap_without_reading_attacker_tree(
    workspace, proposal_factory, tmp_path, monkeypatch
) -> None:
    root = proposal_factory("review_root_swap")
    manifest = _manifest(root, workspace)
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (attacker / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json")) + "\n", encoding="utf-8"
    )
    displaced = tmp_path / "displaced"
    real_enumerate = snapshot_module._enumerate_tree
    swapped = False

    def swap_then_enumerate(root_fd: int, proposal_root):
        nonlocal swapped
        if not swapped:
            swapped = True
            root.rename(displaced)
            attacker.rename(root)
        return real_enumerate(root_fd, proposal_root)

    monkeypatch.setattr(snapshot_module, "_enumerate_tree", swap_then_enumerate)

    with pytest.raises(MemError) as exc:
        capture_snapshot(root, manifest, include_local=False)

    assert exc.value.detail.code == "PROPOSAL_CHANGED_DURING_SNAPSHOT"


@pytest.mark.parametrize("mutation", ["remove", "rename", "rewrite"])
def test_snapshot_detects_every_listed_file_change_after_enumeration(
    workspace, proposal_factory, business_markdown_factory, monkeypatch, mutation
) -> None:
    root = proposal_factory(f"review_tree_{mutation}")
    (root / "evidence").mkdir()
    target = root / "evidence/ev_one.md"
    target.write_bytes(
        business_markdown_factory(
            object_type="evidence",
            record_id="ev_one",
            fields={
                "change_summary": "create",
                "speaker": "user",
                "source_kind": "conversation",
                "user_authored": True,
                "fidelity": "selected_excerpt",
                "status": "active",
            },
        )
    )
    manifest = _manifest(
        root,
        workspace,
        files=[BusinessFile(path="evidence/ev_one.md", operation="create")],
    )
    real_enumerate = snapshot_module._enumerate_tree
    calls = 0

    def mutate_after_first_enumeration(root_fd: int, proposal_root):
        nonlocal calls
        result = real_enumerate(root_fd, proposal_root)
        calls += 1
        if calls == 1:
            if mutation == "remove":
                target.unlink()
            elif mutation == "rename":
                target.rename(root / "evidence/ev_other.md")
            else:
                target.write_bytes(target.read_bytes() + b"changed\n")
        return result

    monkeypatch.setattr(snapshot_module, "_enumerate_tree", mutate_after_first_enumeration)

    with pytest.raises(MemError) as exc:
        capture_snapshot(root, manifest, include_local=False)

    assert exc.value.detail.code in {
        "PROPOSAL_CHANGED_DURING_SNAPSHOT",
        "PROPOSAL_PATH_UNSAFE",
    }


def test_snapshot_input_material_models_are_detached_aliases(
    workspace, proposal_factory
) -> None:
    root = proposal_factory("review_material_alias")
    (root / "inputs").mkdir()
    (root / "inputs/excerpt.txt").write_text("excerpt\n", encoding="utf-8")
    manifest = _manifest(
        root,
        workspace,
        material=[
            ProposalArtifactMaterial(
                material_id="excerpt",
                kind="proposal_artifact",
                path="inputs/excerpt.txt",
                fidelity="selected_excerpt",
            )
        ],
    )

    snapshot = capture_snapshot(root, manifest, include_local=False)
    exposed = snapshot.input_material[0]
    object.__setattr__(exposed.material, "material_id", "attacker")

    assert snapshot.input_material[0].material_id == "excerpt"


def test_artifact_snapshot_budget_accepts_exact_and_rejects_one_byte_less(
    workspace, proposal_factory, monkeypatch
) -> None:
    root = proposal_factory("review_artifact_budget")
    (root / "inputs").mkdir()
    (root / "inputs/excerpt.txt").write_bytes(b"a\r\n")
    manifest = _manifest(
        root,
        workspace,
        material=[
            ProposalArtifactMaterial(
                material_id="excerpt",
                kind="proposal_artifact",
                path="inputs/excerpt.txt",
                fidelity="selected_excerpt",
            )
        ],
    )
    exact = len((root / "manifest.json").read_bytes()) + len(b"a\n")
    monkeypatch.setattr(snapshot_module, "MAX_IMMUTABLE_SNAPSHOT_BYTES", exact)

    assert capture_snapshot(root, manifest, include_local=False).total_bytes == exact
    monkeypatch.setattr(snapshot_module, "MAX_IMMUTABLE_SNAPSHOT_BYTES", exact - 1)
    with pytest.raises(MemError) as exc:
        capture_snapshot(root, manifest, include_local=False)
    assert exc.value.detail.code == "PROPOSAL_SNAPSHOT_TOO_LARGE"
