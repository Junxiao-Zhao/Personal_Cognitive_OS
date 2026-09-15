from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import shutil

from mem_core.errors import MemError
import pytest

from pco.derivation_manifest import verify_derivation_tree
from pco import retrieval
from pco.projections import build_projection
from pco.retrieval import build_retrieval_generation
from pco.verify import verify_projection


@pytest.fixture(autouse=True)
def stub_derivation_backends(monkeypatch):
    def tantivy(path: Path, documents: list[dict]) -> None:
        path.mkdir()
        (path / "meta.json").write_text(
            json.dumps({"count": len(documents)}, sort_keys=True), encoding="utf-8"
        )

    def milvus(path: Path, documents: list[dict]) -> None:
        path.write_text(
            json.dumps({"count": len(documents)}, sort_keys=True), encoding="utf-8"
        )

    monkeypatch.setattr(retrieval, "build_tantivy", tantivy)
    monkeypatch.setattr(retrieval, "build_milvus", milvus)


def _derivation_module():
    return importlib.import_module("pco.derivation")


def _tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _build_projection_at_head(workspace) -> None:
    build_projection(
        workspace.repository,
        workspace.paths.projection,
        workspace.repository.head(),
    )


def _build_indexes_at_head(workspace) -> None:
    build_retrieval_generation(
        workspace.repository,
        workspace.paths.indexes,
        workspace.repository.head(),
    )


def test_first_derive_builds_both_outputs_from_one_head(workspace, monkeypatch) -> None:
    derivation = _derivation_module()
    source_commit = workspace.repository.head()
    observed: list[tuple[str, str]] = []
    real_projection = derivation.build_projection
    real_retrieval = derivation.build_retrieval_generation

    def projection(repository, target, commit):
        observed.append(("projection", commit))
        return real_projection(repository, target, commit)

    def generation(repository, target, commit):
        observed.append(("indexes", commit))
        return real_retrieval(repository, target, commit)

    monkeypatch.setattr(derivation, "build_projection", projection)
    monkeypatch.setattr(derivation, "build_retrieval_generation", generation)

    result = derivation.derive_all(workspace)

    assert result == {
        "ok": True,
        "source_commit": source_commit,
        "projection": "current",
        "index": "current",
    }
    assert observed == [("projection", source_commit), ("indexes", source_commit)]
    assert (
        verify_projection(workspace.paths.projection, expected_commit=source_commit)[
            "ok"
        ]
        is True
    )
    assert (
        verify_derivation_tree(
            workspace.paths.indexes,
            kind="retrieval_index",
            expected_commit=source_commit,
        )["ok"]
        is True
    )
    assert not any(workspace.paths.tmp.iterdir())


def test_build_failure_changes_neither_published_output(workspace, monkeypatch) -> None:
    derivation = _derivation_module()
    source_commit = workspace.repository.head()
    build_projection(workspace.repository, workspace.paths.projection, source_commit)
    build_retrieval_generation(
        workspace.repository, workspace.paths.indexes, source_commit
    )
    before_projection = _tree(workspace.paths.projection)
    before_indexes = _tree(workspace.paths.indexes)

    def fail(*_args, **_kwargs):
        raise MemError(
            "DERIVATION_FAILED",
            "derivation",
            "injected retrieval build failure",
            canonical_changed=False,
            retry_safe=True,
        )

    monkeypatch.setattr(derivation, "build_retrieval_generation", fail)

    with pytest.raises(MemError) as caught:
        derivation.derive_all(workspace)

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert _tree(workspace.paths.projection) == before_projection
    assert _tree(workspace.paths.indexes) == before_indexes
    assert not any(workspace.paths.tmp.iterdir())


def test_unexpected_projection_builder_failure_is_structured_and_publishes_nothing(
    workspace, monkeypatch
) -> None:
    derivation = _derivation_module()

    class InjectedBuilderFailure(Exception):
        pass

    monkeypatch.setattr(
        derivation,
        "build_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            InjectedBuilderFailure("unexpected projection failure")
        ),
    )

    with pytest.raises(MemError) as caught:
        derivation.derive_all(workspace)

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert _tree(workspace.paths.projection) == {}
    assert _tree(workspace.paths.indexes) == {}
    assert not any(workspace.paths.tmp.iterdir())


def test_first_head_change_prevents_either_publication(workspace, monkeypatch) -> None:
    derivation = _derivation_module()
    monkeypatch.setattr(
        derivation, "read_head_before_publish", lambda _workspace: "f" * 40
    )

    with pytest.raises(MemError) as caught:
        derivation.derive_all(workspace)

    assert caught.value.detail.code == "CANONICAL_HEAD_CHANGED"
    assert _tree(workspace.paths.projection) == {}
    assert _tree(workspace.paths.indexes) == {}
    assert not any(workspace.paths.tmp.iterdir())


def test_second_head_change_leaves_new_projection_and_old_indexes(
    workspace, monkeypatch
) -> None:
    derivation = _derivation_module()
    source_commit = workspace.repository.head()
    checks = iter((source_commit, "f" * 40))
    monkeypatch.setattr(
        derivation, "read_head_before_publish", lambda _workspace: next(checks)
    )

    with pytest.raises(MemError) as caught:
        derivation.derive_all(workspace)

    assert caught.value.detail.code == "CANONICAL_HEAD_CHANGED"
    assert (
        verify_projection(workspace.paths.projection, expected_commit=source_commit)[
            "ok"
        ]
        is True
    )
    assert _tree(workspace.paths.indexes) == {}


def test_derive_never_removes_preexisting_tmp_entry(workspace) -> None:
    sentinel = workspace.paths.tmp / "old-crash-remnant"
    sentinel.mkdir()
    (sentinel / "user.txt").write_text("keep\n", encoding="utf-8")

    _derivation_module().derive_all(workspace)

    assert (sentinel / "user.txt").read_text(encoding="utf-8") == "keep\n"
    assert sorted(path.name for path in workspace.paths.tmp.iterdir()) == [
        "old-crash-remnant"
    ]


def test_tmp_parent_rebinding_never_redirects_creation_or_cleanup(
    workspace, tmp_path: Path, monkeypatch
) -> None:
    derivation = _derivation_module()
    real_mkdtemp = derivation.tempfile.mkdtemp
    retained = workspace.paths.root / ".pco/tmp-retained"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_text("keep\n", encoding="utf-8")

    def rebind_then_create(*args, **kwargs):
        workspace.paths.tmp.rename(retained)
        workspace.paths.tmp.symlink_to(outside, target_is_directory=True)
        return real_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(derivation.tempfile, "mkdtemp", rebind_then_create)

    with pytest.raises(MemError) as caught:
        derivation.derive_all(workspace)

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert _tree(outside) == {"sentinel.txt": b"keep\n"}
    assert list(retained.iterdir()) == []


def test_tmp_cleanup_preserves_replacement_at_owned_public_name(
    workspace, monkeypatch
) -> None:
    derivation = _derivation_module()
    retained: list[Path] = []
    replacement: list[Path] = []

    def replace_owned_name_then_fail(_repository, target: Path, _commit: str):
        process_root = target.parent
        moved = process_root.with_name(process_root.name + "-retained")
        process_root.rename(moved)
        process_root.mkdir()
        (process_root / "sentinel.txt").write_text("replacement\n", encoding="utf-8")
        retained.append(workspace.paths.tmp / moved.name)
        replacement.append(workspace.paths.tmp / process_root.name)
        raise RuntimeError("injected after root replacement")

    monkeypatch.setattr(
        derivation, "build_retrieval_generation", replace_owned_name_then_fail
    )

    with pytest.raises(MemError) as caught:
        derivation.derive_all(workspace)

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert _tree(replacement[0]) == {"sentinel.txt": b"replacement\n"}
    assert list(retained[0].iterdir()) == []


def test_projection_conflict_reports_complete_exact_lists_without_publication(
    committed_workspace, monkeypatch
) -> None:
    derivation = _derivation_module()
    projection = committed_workspace.paths.projection
    (projection / "hypotheses/hyp_1.md").write_text("changed\n", encoding="utf-8")
    (projection / "events/evt_1.md").unlink()
    (projection / "manual.txt").write_text("extra\n", encoding="utf-8")
    before_projection = _tree(projection)
    before_indexes = _tree(committed_workspace.paths.indexes)
    monkeypatch.setattr(
        derivation,
        "build_projection",
        lambda *_args, **_kwargs: pytest.fail("conflict must precede construction"),
    )

    with pytest.raises(MemError) as caught:
        derivation.derive_all(committed_workspace)

    assert caught.value.detail.code == "PROJECTION_MODIFIED"
    assert caught.value.detail.value == {
        "modified": ["hypotheses/hyp_1.md"],
        "missing": ["events/evt_1.md"],
        "additional": ["manual.txt"],
    }
    assert _tree(projection) == before_projection
    assert _tree(committed_workspace.paths.indexes) == before_indexes


@pytest.mark.parametrize(
    ("conflict_kind", "expected"),
    [
        (
            "modified",
            {"modified": ["hypotheses/hyp_1.md"], "missing": [], "additional": []},
        ),
        ("missing", {"modified": [], "missing": ["events/evt_1.md"], "additional": []}),
        ("additional", {"modified": [], "missing": [], "additional": ["manual.txt"]}),
    ],
)
def test_each_projection_conflict_class_requires_explicit_discard(
    committed_workspace, conflict_kind: str, expected: dict[str, list[str]]
) -> None:
    projection = committed_workspace.paths.projection
    if conflict_kind == "modified":
        (projection / "hypotheses/hyp_1.md").write_text("changed\n", encoding="utf-8")
    elif conflict_kind == "missing":
        (projection / "events/evt_1.md").unlink()
    else:
        (projection / "manual.txt").write_text("extra\n", encoding="utf-8")

    with pytest.raises(MemError) as caught:
        _derivation_module().derive_all(committed_workspace)

    assert caught.value.detail.code == "PROJECTION_MODIFIED"
    assert caught.value.detail.value == expected


def test_discard_repeats_complete_projection_conflict_then_rebuilds(
    committed_workspace,
) -> None:
    projection = committed_workspace.paths.projection
    (projection / "hypotheses/hyp_1.md").write_text("changed\n", encoding="utf-8")
    (projection / "events/evt_1.md").unlink()
    (projection / "manual.txt").write_text("extra\n", encoding="utf-8")

    result = _derivation_module().derive_all(
        committed_workspace, discard_modified_projection=True
    )

    expected = {
        "modified": ["hypotheses/hyp_1.md"],
        "missing": ["events/evt_1.md"],
        "additional": ["manual.txt"],
    }
    assert result["discarded_projection"] == expected
    assert result["discarded_modified"] == expected["modified"]
    assert result["discarded_missing"] == expected["missing"]
    assert result["discarded_additional"] == expected["additional"]
    assert (
        verify_projection(
            projection, expected_commit=committed_workspace.repository.head()
        )["ok"]
        is True
    )


@pytest.mark.parametrize("state", ["missing", "empty", "current", "stale"])
def test_missing_empty_current_and_stale_projection_are_replaceable(
    workspace, state: str
) -> None:
    projection = workspace.paths.projection
    if state == "missing":
        projection.rmdir()
    elif state in {"current", "stale"}:
        _build_projection_at_head(workspace)
        if state == "stale":
            workspace.repository._git(
                "commit", "--no-verify", "--allow-empty", "-m", "advance head"
            )

    result = _derivation_module().derive_all(workspace)

    assert result["projection"] == "current"
    assert (
        verify_projection(projection, expected_commit=workspace.repository.head())["ok"]
        is True
    )


@pytest.mark.parametrize(
    "unsafe", ["root_symlink", "root_fifo", "inner_symlink", "inner_fifo", "malformed"]
)
def test_unsafe_projection_is_never_followed_or_discarded(
    workspace, tmp_path: Path, unsafe: str
) -> None:
    projection = workspace.paths.projection
    outside = tmp_path / "outside-projection"
    outside.mkdir()
    (outside / "sentinel.txt").write_text("keep\n", encoding="utf-8")
    if unsafe.startswith("root_"):
        projection.rmdir()
        if unsafe == "root_symlink":
            projection.symlink_to(outside, target_is_directory=True)
        else:
            os.mkfifo(projection)
    else:
        if unsafe == "inner_symlink":
            (projection / "unsafe").symlink_to(outside / "sentinel.txt")
        elif unsafe == "inner_fifo":
            os.mkfifo(projection / "unsafe")
        else:
            (projection / "manifest.json").write_bytes(b"not-json\n")

    with pytest.raises(MemError) as caught:
        _derivation_module().derive_all(workspace, discard_modified_projection=True)

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert _tree(outside) == {"sentinel.txt": b"keep\n"}


@pytest.mark.parametrize("state", ["missing", "empty", "valid", "stale", "broken"])
def test_every_real_directory_indexes_state_is_wholly_replaceable(
    workspace, tmp_path: Path, state: str
) -> None:
    indexes = workspace.paths.indexes
    if state == "missing":
        indexes.rmdir()
    elif state in {"valid", "stale"}:
        _build_indexes_at_head(workspace)
        if state == "stale":
            workspace.repository._git(
                "commit", "--no-verify", "--allow-empty", "-m", "advance head"
            )
    elif state == "broken":
        (indexes / "broken.txt").write_text("replace\n", encoding="utf-8")
        outside = tmp_path / "outside-index"
        outside.write_text("keep\n", encoding="utf-8")
        (indexes / "unsafe-link").symlink_to(outside)

    result = _derivation_module().derive_all(workspace)

    assert result["index"] == "current"
    assert (
        verify_derivation_tree(
            indexes,
            kind="retrieval_index",
            expected_commit=workspace.repository.head(),
        )["ok"]
        is True
    )
    assert not (indexes / "active.json").exists()
    assert not (indexes / "generations").exists()


def test_indexes_replacement_during_build_is_not_reauthorized_or_deleted(
    workspace, monkeypatch
) -> None:
    derivation = _derivation_module()
    real_verify = derivation.verify_generation
    retained = workspace.paths.root / ".pco/indexes-retained"
    replacement = workspace.paths.indexes

    def verify_then_replace(*args, **kwargs):
        result = real_verify(*args, **kwargs)
        replacement.rename(retained)
        replacement.mkdir()
        (replacement / "sentinel.txt").write_text("replacement\n", encoding="utf-8")
        return result

    monkeypatch.setattr(derivation, "verify_generation", verify_then_replace)

    with pytest.raises(MemError) as caught:
        derivation.derive_all(workspace)

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert _tree(replacement) == {"sentinel.txt": b"replacement\n"}
    assert list(retained.iterdir()) == []


@pytest.mark.parametrize("unsafe", ["symlink", "fifo"])
def test_indexes_symlink_or_special_root_is_rejected_before_projection_publish(
    workspace, tmp_path: Path, unsafe: str
) -> None:
    indexes = workspace.paths.indexes
    indexes.rmdir()
    outside = tmp_path / "outside-index-root"
    outside.mkdir()
    (outside / "sentinel.txt").write_text("keep\n", encoding="utf-8")
    if unsafe == "symlink":
        indexes.symlink_to(outside, target_is_directory=True)
    else:
        os.mkfifo(indexes)

    with pytest.raises(MemError) as caught:
        _derivation_module().derive_all(workspace)

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert _tree(workspace.paths.projection) == {}
    assert _tree(outside) == {"sentinel.txt": b"keep\n"}


@pytest.mark.parametrize("verification", ["projection", "indexes"])
def test_verification_failure_changes_neither_published_output(
    workspace, monkeypatch, verification: str
) -> None:
    derivation = _derivation_module()
    if verification == "projection":
        monkeypatch.setattr(
            derivation,
            "verify_projection",
            lambda *_args, **_kwargs: {"ok": False, "status": "modified"},
        )
    else:
        monkeypatch.setattr(
            derivation,
            "verify_generation",
            lambda *_args, **_kwargs: {"ok": False, "status": "modified"},
        )

    with pytest.raises(MemError) as caught:
        derivation.derive_all(workspace)

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert _tree(workspace.paths.projection) == {}
    assert _tree(workspace.paths.indexes) == {}


@pytest.mark.parametrize("failure_number", [1, 2])
def test_publication_failure_leaves_only_allowed_missing_or_mixed_state(
    workspace, monkeypatch, failure_number: int
) -> None:
    derivation = _derivation_module()
    real_rename = derivation.atomic_noreplace_rename
    calls = 0

    def fail_selected(source_parent_fd, source_name, target_parent_fd, target_name):
        nonlocal calls
        if source_name == target_name and source_name in {"PCO_MEMORY", "indexes"}:
            calls += 1
            if calls == failure_number:
                raise OSError("injected publication failure")
        return real_rename(source_parent_fd, source_name, target_parent_fd, target_name)

    monkeypatch.setattr(derivation, "atomic_noreplace_rename", fail_selected)

    with pytest.raises(MemError) as caught:
        derivation.derive_all(workspace)

    assert caught.value.detail.code == "DERIVATION_FAILED"
    if failure_number == 1:
        assert not workspace.paths.projection.exists()
        assert workspace.paths.indexes.is_dir()
        assert _tree(workspace.paths.indexes) == {}
    else:
        assert (
            verify_projection(
                workspace.paths.projection,
                expected_commit=workspace.repository.head(),
            )["ok"]
            is True
        )
        assert not workspace.paths.indexes.exists()


def test_no_replace_publication_preserves_a_racing_replacement(
    workspace, monkeypatch
) -> None:
    derivation = _derivation_module()
    real_rename = derivation.atomic_noreplace_rename
    replacement: list[Path] = []

    def collide(source_parent_fd, source_name, target_parent_fd, target_name):
        if source_name == target_name and source_name == "PCO_MEMORY":
            target = Path(f"/proc/self/fd/{target_parent_fd}") / target_name
            target.mkdir()
            (target / "sentinel.txt").write_text("replacement\n", encoding="utf-8")
            replacement.append(workspace.paths.projection)
        return real_rename(source_parent_fd, source_name, target_parent_fd, target_name)

    monkeypatch.setattr(derivation, "atomic_noreplace_rename", collide)

    with pytest.raises(MemError) as caught:
        derivation.derive_all(workspace)

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert _tree(replacement[0]) == {"sentinel.txt": b"replacement\n"}
    assert _tree(workspace.paths.indexes) == {}


def test_publication_detach_never_deletes_target_replacement(
    workspace, monkeypatch
) -> None:
    derivation = _derivation_module()
    _build_projection_at_head(workspace)
    real_rename = derivation.atomic_noreplace_rename
    retained = workspace.paths.projection.with_name("PCO_MEMORY-retained")
    replacement_fd: int | None = None
    attacked = False

    def replace_before_detach(
        source_parent_fd, source_name, target_parent_fd, target_name
    ):
        nonlocal attacked, replacement_fd
        source_parent = Path(f"/proc/self/fd/{source_parent_fd}").resolve()
        if (
            not attacked
            and source_parent == workspace.paths.projection.parent.resolve()
            and source_name == workspace.paths.projection.name
        ):
            attacked = True
            workspace.paths.projection.rename(retained)
            workspace.paths.projection.mkdir()
            replacement_fd = os.open(
                workspace.paths.projection,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
        return real_rename(source_parent_fd, source_name, target_parent_fd, target_name)

    monkeypatch.setattr(derivation, "atomic_noreplace_rename", replace_before_detach)
    try:
        with pytest.raises(MemError) as caught:
            derivation.derive_all(workspace)

        assert attacked is True
        assert caught.value.detail.code == "DERIVATION_FAILED"
        assert replacement_fd is not None
        assert os.fstat(replacement_fd).st_nlink > 0
        assert retained.is_dir()
    finally:
        if replacement_fd is not None:
            os.close(replacement_fd)


def test_process_root_detach_never_deletes_public_name_replacement(
    workspace, monkeypatch
) -> None:
    derivation = _derivation_module()
    real_rename = derivation.atomic_noreplace_rename
    retained: Path | None = None
    replacement_fd: int | None = None
    attacked = False

    def replace_before_detach(
        source_parent_fd, source_name, target_parent_fd, target_name
    ):
        nonlocal attacked, retained, replacement_fd
        source_parent = Path(f"/proc/self/fd/{source_parent_fd}").resolve()
        if (
            not attacked
            and source_parent == workspace.paths.tmp.resolve()
            and source_name.startswith("derive-")
        ):
            attacked = True
            public = workspace.paths.tmp / source_name
            retained = public.with_name(public.name + "-retained")
            public.rename(retained)
            public.mkdir()
            replacement_fd = os.open(
                public, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
        return real_rename(source_parent_fd, source_name, target_parent_fd, target_name)

    monkeypatch.setattr(derivation, "atomic_noreplace_rename", replace_before_detach)
    try:
        with pytest.raises(MemError) as caught:
            derivation.derive_all(workspace)

        assert attacked is True
        assert caught.value.detail.code == "DERIVATION_FAILED"
        assert replacement_fd is not None
        assert os.fstat(replacement_fd).st_nlink > 0
        assert retained is not None and retained.is_dir()
    finally:
        if replacement_fd is not None:
            os.close(replacement_fd)


def test_derive_cleanup_preserves_a_nested_post_capture_name(
    workspace, monkeypatch
) -> None:
    derivation = _derivation_module()
    real_rename = derivation.atomic_noreplace_rename
    real_open = derivation.os.open
    cleanup_remnant: Path | None = None
    inserted = False
    descriptors_before = len(os.listdir("/proc/self/fd"))

    def fail_publication_then_observe_cleanup(
        source_parent_fd, source_name, target_parent_fd, target_name
    ):
        nonlocal cleanup_remnant
        if source_name == target_name == "PCO_MEMORY":
            raise OSError("injected publication failure")
        result = real_rename(
            source_parent_fd, source_name, target_parent_fd, target_name
        )
        source_parent = Path(f"/proc/self/fd/{source_parent_fd}").resolve()
        if source_parent == workspace.paths.tmp.resolve() and source_name.startswith(
            "derive-"
        ):
            cleanup_remnant = workspace.paths.tmp / target_name
        return result

    def insert_after_projection_open(path, flags, *args, **kwargs):
        nonlocal inserted
        descriptor = real_open(path, flags, *args, **kwargs)
        if path == "PCO_MEMORY" and cleanup_remnant is not None and not inserted:
            inserted = True
            nested_fd = real_open(
                "post-capture.txt",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=descriptor,
            )
            try:
                os.write(nested_fd, b"preserve post-capture payload\n")
            finally:
                os.close(nested_fd)
        return descriptor

    monkeypatch.setattr(
        derivation, "atomic_noreplace_rename", fail_publication_then_observe_cleanup
    )
    monkeypatch.setattr(derivation.os, "open", insert_after_projection_open)

    with pytest.raises(MemError) as caught:
        derivation.derive_all(workspace)

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert inserted is True
    assert cleanup_remnant is not None
    assert (cleanup_remnant / "PCO_MEMORY/post-capture.txt").read_bytes() == (
        b"preserve post-capture payload\n"
    )
    assert not (cleanup_remnant / "indexes").exists()
    assert len(os.listdir("/proc/self/fd")) == descriptors_before


def test_recursive_cleanup_never_authorizes_a_nested_post_capture_name(
    tmp_path: Path, monkeypatch
) -> None:
    derivation = _derivation_module()
    root = tmp_path / "cleanup-root"
    root.mkdir()
    child = root / "a-child"
    child.mkdir()
    owned_sibling = root / "z-owned.txt"
    owned_sibling.write_text("erase owned sibling\n", encoding="utf-8")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    real_open = derivation.os.open
    inserted = False
    descriptors_before = len(os.listdir("/proc/self/fd"))

    def insert_after_child_open(path, flags, *args, **kwargs):
        nonlocal inserted
        descriptor = real_open(path, flags, *args, **kwargs)
        if path == "a-child" and kwargs.get("dir_fd") == root_fd and not inserted:
            inserted = True
            nested_fd = real_open(
                "post-capture.txt",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=descriptor,
            )
            try:
                os.write(nested_fd, b"preserve post-capture payload\n")
            finally:
                os.close(nested_fd)
        return descriptor

    monkeypatch.setattr(derivation.os, "open", insert_after_child_open)
    try:
        with pytest.raises(OSError, match="could not be emptied"):
            derivation._erase_owned_directory(root_fd)
    finally:
        os.close(root_fd)

    assert inserted is True
    assert (child / "post-capture.txt").read_bytes() == (
        b"preserve post-capture payload\n"
    )
    assert not owned_sibling.exists()
    assert len(os.listdir("/proc/self/fd")) == descriptors_before - 1
