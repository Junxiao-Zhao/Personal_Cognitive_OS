from __future__ import annotations

import hashlib
from pathlib import Path

from .errors import MemError, ensure


def _profile_files(root: Path, *, phase: str) -> list[tuple[Path, bytes]]:
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise MemError(
            "PROFILE_TREE_UNAVAILABLE",
            phase,
            f"Profile tree is not available: {root}",
            path=str(root),
            canonical_changed=False,
            retry_safe=False,
        ) from exc
    ensure(
        not root.is_symlink() and resolved.is_dir(),
        "PROFILE_TREE_UNSAFE",
        phase,
        f"Profile tree must be a real directory: {root}",
        path=str(root),
        canonical_changed=False,
        retry_safe=False,
    )

    result: list[tuple[Path, bytes]] = []
    for entry in sorted(
        resolved.rglob("*"), key=lambda item: item.relative_to(resolved).as_posix()
    ):
        relative = entry.relative_to(resolved)
        try:
            entry.lstat()
        except OSError as exc:
            raise MemError(
                "PROFILE_TREE_UNAVAILABLE",
                phase,
                f"Profile tree entry is not available: {relative.as_posix()}",
                path=relative.as_posix(),
                canonical_changed=False,
                retry_safe=False,
            ) from exc
        if entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
            raise MemError(
                "PROFILE_TREE_UNSAFE",
                phase,
                f"Profile tree contains a non-regular entry: {relative.as_posix()}",
                path=relative.as_posix(),
                canonical_changed=False,
                retry_safe=False,
            )
        if entry.is_dir():
            continue
        try:
            content = entry.read_bytes()
        except OSError as exc:
            raise MemError(
                "PROFILE_TREE_UNAVAILABLE",
                phase,
                f"Profile tree entry cannot be read: {relative.as_posix()}",
                path=relative.as_posix(),
                canonical_changed=False,
                retry_safe=False,
            ) from exc
        result.append((relative, content))
    return result


def _digest(files: list[tuple[Path, bytes]]) -> str:
    digest = hashlib.sha256()
    for relative, content in files:
        digest.update(b"file\0")
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def profile_tree_digest(root: Path, *, phase: str = "profile_load") -> str:
    return _digest(_profile_files(root, phase=phase))


def snapshot_profile_tree(
    root: Path, target: Path, *, phase: str = "profile_load"
) -> str:
    files = _profile_files(root, phase=phase)
    for relative, content in files:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    return _digest(files)
