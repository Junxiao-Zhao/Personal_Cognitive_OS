from __future__ import annotations

import json
import os
import re
import socket
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from mem_core.errors import MemError
from mem_core.repository import MemoryRepository
from pco.config import PROFILE_VERSION, WORKSPACE_FORMAT_VERSION
from pco.paths import bundled_profile
from pco.skill_resources import bundled_skill, verify_skill_tree


def _workspace_api():
    from pco import init_safety
    from pco import workspace as workspace_module

    return init_safety, workspace_module


def _make_entry(path: Path, kind: str) -> Path:
    if kind == "symlink":
        destination = path.parent / "destination"
        destination.mkdir()
        path.symlink_to(destination, target_is_directory=True)
    elif kind == "file":
        path.write_bytes(b"do not replace")
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "socket":
        listener = socket.socket(socket.AF_UNIX)
        try:
            listener.bind(str(path))
        finally:
            listener.close()
    else:  # pragma: no cover - test helper misuse
        raise AssertionError(f"unknown test entry kind: {kind}")
    return path


def _snapshot_parent(parent: Path) -> tuple[tuple[str, int, bytes | str | None], ...]:
    result: list[tuple[str, int, bytes | str | None]] = []
    with os.scandir(parent) as entries:
        for entry in entries:
            observed = entry.stat(follow_symlinks=False)
            payload: bytes | str | None = None
            if stat.S_ISLNK(observed.st_mode):
                payload = os.readlink(entry.path)
            elif stat.S_ISREG(observed.st_mode):
                payload = Path(entry.path).read_bytes()
            result.append((entry.name, observed.st_mode, payload))
    return tuple(sorted(result))


def _fake_stat_result(mode: int) -> os.stat_result:
    return os.stat_result((mode, 0, 0, 1, 0, 0, 0, 0, 0, 0))


def _root_commit_paths(repository: MemoryRepository) -> set[str]:
    root_commit = repository._git("rev-list", "--max-parents=0", "HEAD")
    output = repository._git("ls-tree", "-r", "--name-only", root_commit)
    return set(output.splitlines())


def _assert_trees_equal(source: Path, target: Path) -> None:
    source_files = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    target_files = {
        path.relative_to(target).as_posix(): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    assert target_files == source_files


def test_canonical_json_bytes_is_compact_utf8_sorted_and_lf_terminated() -> None:
    init_safety, _ = _workspace_api()

    assert init_safety.canonical_json_bytes({"z": "雪", "a": 1}) == (
        b'{"a":1,"z":"\xe9\x9b\xaa"}\n'
    )


def test_lstat_final_observes_a_symlink_instead_of_following_it(tmp_path: Path) -> None:
    init_safety, _ = _workspace_api()
    destination = tmp_path / "destination"
    destination.mkdir()
    link = tmp_path / "link"
    link.symlink_to(destination, target_is_directory=True)

    observed = init_safety.lstat_final(link)

    assert observed is not None
    assert stat.S_ISLNK(observed.st_mode)


@pytest.mark.parametrize("kind", ["symlink", "file", "fifo", "socket"])
def test_init_rejects_unsafe_final_component_without_mutation(
    tmp_path: Path, kind: str
) -> None:
    _, workspace_module = _workspace_api()
    target = _make_entry(tmp_path / "target", kind)
    before = _snapshot_parent(tmp_path)

    with pytest.raises(MemError) as exc:
        workspace_module.Workspace.init(target)

    assert exc.value.detail.code == "WORKSPACE_PATH_UNSAFE"
    assert _snapshot_parent(tmp_path) == before


def test_init_never_resolves_or_follows_the_target_final_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, workspace_module = _workspace_api()
    destination = tmp_path / "destination"
    destination.mkdir()
    target = tmp_path / "target"
    target.symlink_to(destination, target_is_directory=True)
    real_resolve = Path.resolve

    def reject_target_resolution(path: Path, *args, **kwargs):
        if path == target or target in path.parents:
            raise AssertionError("workspace target must not be resolved")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", reject_target_resolution)

    with pytest.raises(MemError) as exc:
        workspace_module.Workspace.init(target)

    assert exc.value.detail.code == "WORKSPACE_PATH_UNSAFE"
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize(
    "mode",
    [stat.S_IFBLK | 0o600, stat.S_IFCHR | 0o600, 0o150000 | 0o600],
)
def test_init_rejects_device_and_unknown_special_modes_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    init_safety, workspace_module = _workspace_api()
    target = tmp_path / "target"
    before = _snapshot_parent(tmp_path)
    monkeypatch.setattr(init_safety, "lstat_final", lambda _path: _fake_stat_result(mode))

    with pytest.raises(MemError) as exc:
        workspace_module.Workspace.init(target)

    assert exc.value.detail.code == "WORKSPACE_PATH_UNSAFE"
    assert _snapshot_parent(tmp_path) == before


def test_init_rejects_nonempty_real_directory_without_mutation(tmp_path: Path) -> None:
    _, workspace_module = _workspace_api()
    target = tmp_path / "target"
    target.mkdir()
    marker = target / "owned.txt"
    marker.write_text("owned\n", encoding="utf-8")
    before = _snapshot_parent(target)

    with pytest.raises(MemError) as exc:
        workspace_module.Workspace.init(target)

    assert exc.value.detail.code == "WORKSPACE_NOT_EMPTY"
    assert _snapshot_parent(target) == before


@pytest.mark.parametrize("precreate", [False, True], ids=["target", "pco-claim"])
def test_missing_anchor_capability_is_rejected_before_any_target_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, precreate: bool
) -> None:
    init_safety, workspace_module = _workspace_api()
    target = tmp_path / "target"
    if precreate:
        target.mkdir()

    def unavailable() -> None:
        raise MemError(
            "WORKSPACE_PATH_UNSAFE",
            "init",
            "descriptor operations unavailable",
            canonical_changed=False,
            retry_safe=False,
        )

    monkeypatch.setattr(
        init_safety, "require_anchor_capabilities_before_mutation", unavailable
    )

    with pytest.raises(MemError) as exc:
        workspace_module.Workspace.init(target)

    assert exc.value.detail.code == "WORKSPACE_PATH_UNSAFE"
    if precreate:
        assert list(target.iterdir()) == []
    else:
        assert not target.exists()


def test_missing_fd_enumeration_capability_is_rejected_before_target_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_safety, workspace_module = _workspace_api()
    target = tmp_path / "target"
    monkeypatch.setattr(init_safety.os, "supports_fd", set())

    with pytest.raises(MemError) as exc:
        workspace_module.Workspace.init(target)

    assert exc.value.detail.code == "WORKSPACE_PATH_UNSAFE"
    assert not target.exists()


def test_losing_initializer_never_cleans_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_safety, workspace_module = _workspace_api()
    target = tmp_path / "workspace"
    target.mkdir()
    real_mkdir = init_safety.os.mkdir

    def let_other_initializer_win(path, mode=0o777, *, dir_fd=None):
        if path == ".pco" and dir_fd is not None:
            real_mkdir(path, mode=mode, dir_fd=dir_fd)
            claim_fd = os.open(
                ".pco",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=dir_fd,
            )
            try:
                winner_fd = os.open(
                    "winner",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=claim_fd,
                )
                try:
                    os.write(winner_fd, b"winner")
                finally:
                    os.close(winner_fd)
            finally:
                os.close(claim_fd)
            raise FileExistsError("simulated competing initializer")
        return real_mkdir(path, mode=mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        init_safety, "require_anchor_capabilities_before_mutation", lambda: None
    )
    monkeypatch.setattr(init_safety.os, "mkdir", let_other_initializer_win)

    with pytest.raises(MemError) as exc:
        workspace_module.Workspace.init(target)

    assert exc.value.detail.code == "WORKSPACE_INIT_RACE"
    assert (target / ".pco/winner").read_text(encoding="utf-8") == "winner"


def test_failed_initializer_preserves_its_partial_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, workspace_module = _workspace_api()
    target = tmp_path / "workspace"

    def fail_after_write(repository, root, **_kwargs):
        root.write_bytes("partial", b"preserve me")
        raise MemError(
            "GIT_FAILED",
            "git",
            "simulated failure",
            canonical_changed=False,
            retry_safe=True,
        )

    monkeypatch.setattr(MemoryRepository, "init_anchored", fail_after_write)

    with pytest.raises(MemError):
        workspace_module.Workspace.init(target)

    assert (target / ".pco/canonical/partial").read_bytes() == b"preserve me"
    with pytest.raises(MemError) as exc:
        workspace_module.Workspace.init(target)
    assert exc.value.detail.code == "WORKSPACE_NOT_EMPTY"
    assert (target / ".pco/canonical/partial").read_bytes() == b"preserve me"


def test_target_swap_after_pco_claim_never_writes_through_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, workspace_module = _workspace_api()
    target = tmp_path / "workspace"
    target.mkdir()
    moved = tmp_path / "moved-original"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_claim = workspace_module.claim_workspace

    @contextmanager
    def swap_after_claim(path: Path) -> Iterator[object]:
        with real_claim(path) as anchor:
            path.rename(moved)
            path.symlink_to(outside, target_is_directory=True)
            yield anchor

    monkeypatch.setattr(workspace_module, "claim_workspace", swap_after_claim)

    with pytest.raises(MemError) as exc:
        workspace_module.Workspace.init(target)

    assert exc.value.detail.code == "WORKSPACE_INIT_RACE"
    assert list(outside.iterdir()) == []
    assert (moved / ".pco").is_dir()


def test_claimed_pco_swap_is_detected_and_never_writes_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_safety, workspace_module = _workspace_api()
    target = tmp_path / "workspace"
    target.mkdir()
    claimed = target / ".pco-claimed"
    replacement = target / ".pco"
    real_open_directory = init_safety.DirectoryAnchor.open_directory
    swapped = False

    def swap_after_pco_open(anchor, relative):
        nonlocal swapped
        child = real_open_directory(anchor, relative)
        if str(relative) == ".pco" and not swapped:
            replacement.rename(claimed)
            replacement.mkdir()
            swapped = True
        return child

    monkeypatch.setattr(
        init_safety.DirectoryAnchor, "open_directory", swap_after_pco_open
    )

    with pytest.raises(MemError) as exc:
        workspace_module.Workspace.init(target)

    assert exc.value.detail.code == "WORKSPACE_INIT_RACE"
    assert list(replacement.iterdir()) == []
    assert claimed.is_dir()


def test_target_swap_during_repository_init_keeps_git_writes_on_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, workspace_module = _workspace_api()
    target = tmp_path / "workspace"
    target.mkdir()
    moved = tmp_path / "moved-original"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_init = MemoryRepository.init_anchored

    def swap_then_init(repository, root, **kwargs):
        target.rename(moved)
        target.symlink_to(outside, target_is_directory=True)
        return real_init(repository, root, **kwargs)

    monkeypatch.setattr(MemoryRepository, "init_anchored", swap_then_init)

    with pytest.raises(MemError) as exc:
        workspace_module.Workspace.init(target)

    assert exc.value.detail.code == "WORKSPACE_INIT_RACE"
    assert list(outside.iterdir()) == []
    assert (moved / ".pco/canonical/.git").is_dir()


@pytest.mark.parametrize("precreate", [False, True], ids=["absent", "empty"])
def test_init_builds_complete_canonical_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    precreate: bool,
) -> None:
    _, workspace_module = _workspace_api()
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_home / "missing-gitconfig"))
    target = tmp_path / "workspace"
    if precreate:
        target.mkdir()

    def forbidden_path_initializer(*_args, **_kwargs):
        raise AssertionError("pco init must use the anchored initializer")

    monkeypatch.setattr(MemoryRepository, "init", forbidden_path_initializer)
    monkeypatch.setattr(
        MemoryRepository, "install_pre_commit_hook", forbidden_path_initializer
    )

    result = workspace_module.Workspace.init(target)
    workspace = workspace_module.Workspace.open(target)
    repository = workspace.repository

    assert result == {
        "ok": True,
        "workspace": str(target.absolute()),
        "workspace_id": workspace.identity["workspace_id"],
        "memory_commit": repository.head(),
    }
    assert repository._git("branch", "--show-current") == "main"
    assert repository._git("config", "--local", "user.name") == "PCO Memory"
    assert repository._git("config", "--local", "user.email") == "pco-memory@local"
    assert all(
        repository.profile.stream_path(repository.root, name).read_bytes() == b""
        for name in repository.profile.config.streams
    )
    assert (repository.root / "transactions/transactions.jsonl").read_bytes() == b""
    assert (repository.root / "source_blobs/.gitkeep").read_bytes() == b""
    assert json.loads((repository.root / ".mem-profile.json").read_text()) == {
        "name": "pco",
        "path": "profiles/pco",
        "version": PROFILE_VERSION,
    }
    assert (repository.root / ".gitignore").read_text(encoding="utf-8") == "*.lock\n"
    assert repository.pre_commit_hook_status()["installed"] is True

    identity = workspace.identity
    assert re.fullmatch(r"workspace_[0-9a-f]{32}", identity["workspace_id"])
    assert identity == {
        "profile_version": PROFILE_VERSION,
        "workspace_format_version": WORKSPACE_FORMAT_VERSION,
        "workspace_id": identity["workspace_id"],
    }
    expected_identity = (
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    assert (repository.root / "workspace.json").read_bytes() == expected_identity

    expected_commit_paths = {
        ".gitignore",
        ".mem-profile.json",
        "source_blobs/.gitkeep",
        "transactions/transactions.jsonl",
        "workspace.json",
    }
    expected_commit_paths.update(
        stream.path for stream in repository.profile.config.streams.values()
    )
    expected_commit_paths.update(
        f"profiles/pco/{path.relative_to(bundled_profile()).as_posix()}"
        for path in bundled_profile().rglob("*")
        if path.is_file()
    )
    assert _root_commit_paths(repository) == expected_commit_paths

    for skill_name in ("pco-memory", "pco-consolidate"):
        installed = workspace.paths.skills / skill_name
        verify_skill_tree(installed, "0.5.0")
        _assert_trees_equal(Path(str(bundled_skill(skill_name))), installed)
    assert {path.name for path in workspace.paths.skills.iterdir()} == {
        "pco-memory",
        "pco-consolidate",
    }

    for path in (
        workspace.paths.indexes,
        workspace.paths.tmp,
        workspace.paths.proposals,
        workspace.paths.projection,
    ):
        assert path.is_dir()
        assert list(path.iterdir()) == []


def test_production_workspace_hook_rejects_workspace_identity_change(
    tmp_path: Path,
) -> None:
    _, workspace_module = _workspace_api()
    target = tmp_path / "workspace"
    workspace_module.Workspace.init(target)
    workspace = workspace_module.Workspace.open(target)
    repository = workspace.repository
    metadata = repository.root / "workspace.json"
    replacement = dict(workspace.identity)
    replacement["workspace_id"] = "workspace_replaced"
    metadata.write_text(
        json.dumps(replacement, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    repository._git("add", "workspace.json")

    result = subprocess.run(
        ["git", "commit", "-m", "replace workspace identity"],
        cwd=repository.root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "WORKSPACE_INTEGRITY_ERROR" in result.stdout + result.stderr


def test_directory_anchor_rejects_escape_paths_for_every_mutation_api(
    tmp_path: Path,
) -> None:
    init_safety, _ = _workspace_api()
    root = tmp_path / "root"
    root.mkdir()
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    with init_safety.DirectoryAnchor(
        root_fd,
        expected_identity=init_safety.fstat_identity(root_fd),
        display_path=root,
    ) as anchor:
        operations = (
            lambda relative: anchor.mkdir(relative),
            lambda relative: anchor.open_directory(relative),
            lambda relative: anchor.touch(relative),
            lambda relative: anchor.write_bytes(relative, b"unsafe"),
            lambda relative: anchor.chmod(relative, 0o600),
            lambda relative: anchor.git_cwd(relative),
        )
        for operation in operations:
            for relative in ("/absolute", "../escape", "child/../../escape"):
                with pytest.raises(MemError) as exc:
                    operation(relative)
                assert exc.value.detail.code == "WORKSPACE_PATH_UNSAFE"


@pytest.mark.parametrize(
    "operation",
    [
        lambda anchor: anchor.mkdir("link"),
        lambda anchor: anchor.open_directory("link"),
        lambda anchor: anchor.touch("link/escape", parents=True),
        lambda anchor: anchor.write_bytes("link/escape", b"unsafe", parents=True),
        lambda anchor: anchor.chmod("link", 0o600),
        lambda anchor: anchor.git_cwd("link"),
    ],
    ids=["mkdir", "open-directory", "touch", "write", "chmod", "git-cwd"],
)
def test_directory_anchor_never_follows_symlink(
    tmp_path: Path, operation
) -> None:
    init_safety, _ = _workspace_api()
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    with init_safety.DirectoryAnchor(
        root_fd,
        expected_identity=init_safety.fstat_identity(root_fd),
        display_path=root,
    ) as anchor:
        with pytest.raises(MemError) as exc:
            operation(anchor)
        assert exc.value.detail.code == "WORKSPACE_PATH_UNSAFE"

    assert list(outside.iterdir()) == []
