from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from mem_core.errors import MemError
from pco.skill_resources import (
    bundled_skill,
    hash_regular_tree,
    verify_skill_tree,
    write_canonical_json,
)


def _sha256(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def make_skill_tree(
    tmp_path: Path,
    files: dict[str, bytes],
    *,
    requires_pco: str = ">=0.5.0,<0.6.0",
) -> Path:
    """Create a manifest-backed SKILL tree from independently hashed bytes."""
    skill = tmp_path / "example-skill"
    skill.mkdir()
    for relative_path, contents in files.items():
        target = skill / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    write_canonical_json(
        skill / "manifest.json",
        {
            "name": "example-skill",
            "skill_set_version": "0.5.0",
            "requires_pco": requires_pco,
            "files": {path: _sha256(contents) for path, contents in files.items()},
        },
    )
    return skill


def make_regular_tree(tmp_path: Path, files: dict[str, bytes]) -> Path:
    tree = tmp_path / "tree"
    tree.mkdir()
    for relative_path, contents in files.items():
        target = tree / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    return tree


def snapshot_tree(root: Path) -> dict[str, tuple[Any, ...]]:
    """Capture every entry without following symlinks for read-only assertions."""
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
                snapshot[relative] = (*metadata, _sha256(entry_path.read_bytes()))
            elif stat.S_ISLNK(info.st_mode):
                snapshot[relative] = (*metadata, os.readlink(entry.path))
            else:
                snapshot[relative] = metadata
            if stat.S_ISDIR(info.st_mode):
                pending.append((entry_path, prefix / entry.name))
    return snapshot


def test_bundled_skill_rejects_a_traversal_name() -> None:
    """Passing a traversal name must not escape the packaged skills resource."""
    with pytest.raises(MemError) as exc:
        bundled_skill("../other")

    assert exc.value.detail.code == "WORKSPACE_SKILL_INVALID"


def test_verify_skill_tree_accepts_a_matching_manifest(tmp_path: Path) -> None:
    """A manifest matching every ordinary file is accepted read-only."""
    skill = make_skill_tree(tmp_path, files={"SKILL.md": b"body\n"})
    before = snapshot_tree(skill)

    assert verify_skill_tree(skill, package_version="0.5.1") == {
        "ok": True,
        "name": "example-skill",
        "version": "0.5.0",
    }
    assert snapshot_tree(skill) == before


def test_verify_skill_tree_accepts_a_manifest_declared_compatible_range(
    tmp_path: Path,
) -> None:
    """Replacing a manifest range with a valid narrower range must stay valid."""
    skill = make_skill_tree(
        tmp_path,
        files={"SKILL.md": b"body\n"},
        requires_pco=">=0.5.0,<0.5.2",
    )

    assert verify_skill_tree(skill, package_version="0.5.1")["ok"] is True


def test_verify_skill_tree_rejects_unlisted_file(tmp_path: Path) -> None:
    """Ignoring files absent from a manifest would execute unreviewed content."""
    skill = make_skill_tree(tmp_path, files={"SKILL.md": b"body\n"})
    (skill / "extra.txt").write_text("unexpected\n")
    before = snapshot_tree(skill)

    with pytest.raises(MemError) as exc:
        verify_skill_tree(skill, package_version="0.5.0")

    assert exc.value.detail.code == "WORKSPACE_SKILL_INVALID"
    assert snapshot_tree(skill) == before


def test_verify_skill_tree_rejects_symlink(tmp_path: Path) -> None:
    """Following a linked resource would let a tree escape its signed contents."""
    skill = make_skill_tree(tmp_path, files={"SKILL.md": b"body\n"})
    (skill / "linked.md").symlink_to(skill / "SKILL.md")
    before = snapshot_tree(skill)

    with pytest.raises(MemError) as exc:
        verify_skill_tree(skill, package_version="0.5.0")

    assert exc.value.detail.code == "WORKSPACE_SKILL_INVALID"
    assert snapshot_tree(skill) == before


def test_verify_skill_tree_rejects_incompatible_package(tmp_path: Path) -> None:
    """Accepting a 0.6 package would cross the v0.5 compatibility boundary."""
    skill = make_skill_tree(tmp_path, files={"SKILL.md": b"body\n"})

    with pytest.raises(MemError) as exc:
        verify_skill_tree(skill, package_version="0.6.0")

    assert exc.value.detail.code == "WORKSPACE_SKILL_INVALID"


def test_verify_skill_tree_rejects_an_invalid_manifest_specifier(tmp_path: Path) -> None:
    """Parsing malformed manifest compatibility syntax must stay structured."""
    skill = make_skill_tree(
        tmp_path,
        files={"SKILL.md": b"body\n"},
        requires_pco="not a specifier",
    )

    with pytest.raises(MemError) as exc:
        verify_skill_tree(skill, package_version="0.5.0")

    assert exc.value.detail.code == "WORKSPACE_SKILL_INVALID"


def test_verify_skill_tree_rejects_unsafe_manifest_path(tmp_path: Path) -> None:
    """A traversal path in a manifest must not normalize into a trusted file."""
    skill = make_skill_tree(tmp_path, files={"SKILL.md": b"body\n"})
    manifest_path = skill / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = {"nested/../SKILL.md": _sha256(b"body\n")}
    write_canonical_json(manifest_path, manifest)

    with pytest.raises(MemError) as exc:
        verify_skill_tree(skill, package_version="0.5.0")

    assert exc.value.detail.code == "WORKSPACE_SKILL_INVALID"


def test_verify_skill_tree_rejects_duplicate_manifest_path(tmp_path: Path) -> None:
    """Collapsing duplicate JSON keys would make the signed tree ambiguous."""
    skill = make_skill_tree(tmp_path, files={"SKILL.md": b"body\n"})
    digest = _sha256(b"body\n")
    (skill / "manifest.json").write_text(
        "{"
        '"name":"example-skill",'
        '"skill_set_version":"0.5.0",'
        '"requires_pco":">=0.5.0,<0.6.0",'
        f'"files":{{"SKILL.md":"{digest}","SKILL.md":"{digest}"}}'
        "}",
        encoding="utf-8",
    )

    with pytest.raises(MemError) as exc:
        verify_skill_tree(skill, package_version="0.5.0")

    assert exc.value.detail.code == "WORKSPACE_SKILL_INVALID"


def test_hash_regular_tree_is_path_sorted_and_deterministic(tmp_path: Path) -> None:
    """Returning filesystem order would make equivalent trees hash differently."""
    skill = make_regular_tree(tmp_path, {"z.md": b"z\n", "a.md": b"a\n"})

    assert list(hash_regular_tree(skill, exclude=set())) == ["a.md", "z.md"]
    assert hash_regular_tree(skill, exclude=set()) == hash_regular_tree(skill, exclude=set())


def test_write_canonical_json_sorts_keys_and_ends_with_a_newline(tmp_path: Path) -> None:
    """Nondeterministic manifest output would create unnecessary integrity diffs."""
    target = tmp_path / "manifest.json"

    write_canonical_json(target, {"z": 1, "a": {"b": 2}})

    assert target.read_text(encoding="utf-8") == '{\n  "a": {\n    "b": 2\n  },\n  "z": 1\n}\n'
