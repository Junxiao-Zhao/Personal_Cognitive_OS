"""Read-only verification for immutable workspace-local SKILL resources."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from mem_core.errors import MemError, ensure
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

_INVALID_CODE = "WORKSPACE_SKILL_INVALID"
_INVALID_PHASE = "verify"


class _JsonObject(dict[str, Any]):
    """A JSON object retaining its raw key sequence for duplicate detection."""

    def __init__(self, pairs: list[tuple[str, Any]]) -> None:
        super().__init__(pairs)
        self.pairs = pairs


def bundled_skill(name: str) -> Traversable:
    """Return a packaged SKILL tree without copying it into a workspace."""
    relative_name = _normalize_relative(name)
    return files("pco.resources.skills").joinpath(relative_name)


def hash_regular_tree(path: Path, *, exclude: set[str]) -> dict[str, str]:
    """Hash a tree of ordinary files, rejecting every non-regular entry.

    The result is inserted in canonical POSIX-path order.  Directory walks use
    ``lstat`` semantics, so a symlink is rejected rather than traversed.
    """
    excluded = {_normalize_relative(item) for item in exclude}
    entries = _regular_files_no_follow(path)
    return {
        relative: _sha256_file(entry)
        for relative, entry in entries
        if relative not in excluded
    }


def write_canonical_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write a stable, human-readable JSON artifact."""
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def verify_skill_tree(path: Path, package_version: str) -> dict[str, Any]:
    """Verify a workspace-local SKILL against its immutable manifest.

    This function only reads the supplied tree; it never repairs, copies, or
    writes resources while checking them.
    """
    _ensure_directory(path)
    manifest = _load_manifest(path)
    observed = hash_regular_tree(path, exclude={"manifest.json"})
    return _validate_skill_manifest(manifest, observed, package_version)


def verified_skill_tree_bytes(
    path: Path, package_version: str
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Read, hash, and validate the exact regular-file bytes to be installed.

    Entry type is checked on ``O_PATH|O_NOFOLLOW`` descriptors. Only an inode
    already proven regular is reopened through its descriptor for reading, so
    a pathname race cannot make verification block on or activate a special
    file. The returned bytes are the same bytes covered by the manifest check.
    """

    snapshot = _read_regular_tree_bytes(path)
    manifest_path = path / "manifest.json"
    try:
        manifest_bytes = snapshot["manifest.json"]
    except KeyError:
        _invalid("Unable to read valid SKILL manifest", path=str(manifest_path))
    manifest = _parse_manifest_bytes(manifest_bytes, manifest_path)
    observed = {
        relative: hashlib.sha256(content).hexdigest()
        for relative, content in snapshot.items()
        if relative != "manifest.json"
    }
    metadata = _validate_skill_manifest(manifest, observed, package_version)
    return metadata, snapshot


def _validate_skill_manifest(
    manifest: Mapping[str, Any],
    observed: Mapping[str, str],
    package_version: str,
) -> dict[str, Any]:
    expected = _manifest_files(manifest)
    _ensure_supported_requirement(manifest.get("requires_pco"), package_version)
    ensure(
        observed == expected,
        _INVALID_CODE,
        _INVALID_PHASE,
        "Workspace-local SKILL files differ from their manifest",
        canonical_changed=False,
        retry_safe=False,
        value=_diff_hashes(expected, observed),
    )
    name = manifest.get("name")
    skill_set_version = manifest.get("skill_set_version")
    ensure(
        isinstance(name, str) and name,
        _INVALID_CODE,
        _INVALID_PHASE,
        "SKILL manifest has no valid name",
        canonical_changed=False,
        retry_safe=False,
    )
    ensure(
        isinstance(skill_set_version, str) and skill_set_version,
        _INVALID_CODE,
        _INVALID_PHASE,
        "SKILL manifest has no valid skill_set_version",
        canonical_changed=False,
        retry_safe=False,
    )
    return {"ok": True, "name": name, "version": skill_set_version}


def _read_regular_tree_bytes(root: Path) -> dict[str, bytes]:
    if not hasattr(os, "O_PATH") or not Path("/proc/self/fd").is_dir():
        _invalid("Safe descriptor-backed SKILL reads are unavailable", path=str(root))
    flags = os.O_PATH | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        path_descriptor = os.open(root, flags)
    except OSError as exc:
        _invalid("Unable to open SKILL resource tree", path=str(root), value=str(exc))
    try:
        root_stat = os.fstat(path_descriptor)
        if not stat.S_ISDIR(root_stat.st_mode):
            _invalid("SKILL resource tree must be a real directory", path=str(root))
        directory_fd = _open_descriptor_for_read(path_descriptor, root_stat, directory=True)
    finally:
        os.close(path_descriptor)
    try:
        snapshot: dict[str, bytes] = {}
        _read_regular_directory(directory_fd, PurePosixPath("."), snapshot)
        return dict(sorted(snapshot.items()))
    finally:
        os.close(directory_fd)


def _read_regular_directory(
    directory_fd: int,
    prefix: PurePosixPath,
    snapshot: dict[str, bytes],
) -> None:
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError as exc:
        _invalid(
            "Unable to enumerate SKILL resource tree",
            path=prefix.as_posix(),
            value=str(exc),
        )
    for name in names:
        relative = _normalize_relative((prefix / name).as_posix())
        flags = os.O_PATH | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            path_descriptor = os.open(name, flags, dir_fd=directory_fd)
        except OSError as exc:
            _invalid(
                "Unable to open SKILL resource entry",
                path=relative,
                value=str(exc),
            )
        try:
            observed = os.fstat(path_descriptor)
            if stat.S_ISDIR(observed.st_mode):
                child_fd = _open_descriptor_for_read(
                    path_descriptor, observed, directory=True
                )
                try:
                    _read_regular_directory(
                        child_fd, PurePosixPath(relative), snapshot
                    )
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(observed.st_mode):
                content_fd = _open_descriptor_for_read(
                    path_descriptor, observed, directory=False
                )
                try:
                    snapshot[relative] = _read_all(content_fd)
                finally:
                    os.close(content_fd)
            else:
                _invalid(
                    "SKILL resource tree contains a non-regular file",
                    path=relative,
                )
        finally:
            os.close(path_descriptor)


def _open_descriptor_for_read(
    path_descriptor: int,
    expected: os.stat_result,
    *,
    directory: bool,
) -> int:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    if directory:
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(f"/proc/self/fd/{path_descriptor}", flags)
    except OSError as exc:
        _invalid("Unable to read opened SKILL resource entry", value=str(exc))
    try:
        actual = os.fstat(descriptor)
        expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
        if (
            not expected_kind(actual.st_mode)
            or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            _invalid("Opened SKILL resource entry identity changed")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        try:
            chunk = os.read(descriptor, 1024 * 1024)
        except OSError as exc:
            _invalid("Unable to read opened SKILL resource entry", value=str(exc))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _regular_files_no_follow(root: Path) -> list[tuple[str, Path]]:
    _ensure_directory(root)
    entries: list[tuple[str, Path]] = []
    pending: list[tuple[Path, PurePosixPath]] = [(root, PurePosixPath("."))]
    while pending:
        directory, prefix = pending.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            _invalid(
                "Unable to enumerate SKILL resource tree",
                path=str(directory),
                value=str(exc),
            )
        for child in children:
            relative = _normalize_relative((prefix / child.name).as_posix())
            try:
                mode = child.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                _invalid(
                    "Unable to inspect SKILL resource entry",
                    path=relative,
                    value=str(exc),
                )
            if stat.S_ISLNK(mode):
                _invalid("SKILL resource tree contains a symlink", path=relative)
            child_path = Path(child.path)
            if stat.S_ISDIR(mode):
                pending.append((child_path, PurePosixPath(relative)))
            elif stat.S_ISREG(mode):
                entries.append((relative, child_path))
            else:
                _invalid(
                    "SKILL resource tree contains a non-regular file",
                    path=relative,
                )
    return sorted(entries, key=lambda item: item[0])


def _sha256_file(path: Path) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _invalid("Unable to read SKILL resource file", path=str(path), value=str(exc))
    try:
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode):
            _invalid(
                "SKILL resource tree contains a non-regular file", path=str(path)
            )
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        _invalid("Unable to hash SKILL resource file", path=str(path), value=str(exc))
    finally:
        os.close(descriptor)


def _load_manifest(path: Path) -> dict[str, Any]:
    manifest_path = path / "manifest.json"
    try:
        raw = _read_regular_file(manifest_path)
    except OSError as exc:
        _invalid(
            "Unable to read valid SKILL manifest",
            path=str(manifest_path),
            value=str(exc),
        )
    return _parse_manifest_bytes(raw, manifest_path)


def _parse_manifest_bytes(raw: bytes, manifest_path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_JsonObject)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _invalid(
            "Unable to read valid SKILL manifest",
            path=str(manifest_path),
            value=str(exc),
        )
    ensure(
        isinstance(parsed, _JsonObject),
        _INVALID_CODE,
        _INVALID_PHASE,
        "SKILL manifest must be a JSON object",
        canonical_changed=False,
        retry_safe=False,
    )
    _ensure_unique_json_keys(parsed, "SKILL manifest")
    return parsed


def _read_regular_file(path: Path) -> bytes:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        _invalid("Unable to inspect SKILL manifest", path=str(path), value=str(exc))
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        _invalid("SKILL manifest must be a regular file", path=str(path))
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _invalid("Unable to read SKILL manifest", path=str(path), value=str(exc))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            _invalid("SKILL manifest must be a regular file", path=str(path))
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _manifest_files(manifest: Mapping[str, Any]) -> dict[str, str]:
    raw_files = manifest.get("files")
    ensure(
        isinstance(raw_files, dict),
        _INVALID_CODE,
        _INVALID_PHASE,
        "SKILL manifest files must be an object",
        canonical_changed=False,
        retry_safe=False,
    )
    files_by_path: dict[str, str] = {}
    entries = raw_files.pairs if isinstance(raw_files, _JsonObject) else raw_files.items()
    for raw_path, digest in entries:
        ensure(
            isinstance(raw_path, str) and isinstance(digest, str),
            _INVALID_CODE,
            _INVALID_PHASE,
            "SKILL manifest files must map paths to hashes",
            canonical_changed=False,
            retry_safe=False,
        )
        normalized = _normalize_relative(raw_path)
        ensure(
            normalized not in files_by_path,
            _INVALID_CODE,
            _INVALID_PHASE,
            "SKILL manifest contains duplicate normalized paths",
            canonical_changed=False,
            retry_safe=False,
            path=normalized,
        )
        files_by_path[normalized] = digest
    return dict(sorted(files_by_path.items()))


def _ensure_supported_requirement(requirement: Any, package_version: str) -> None:
    try:
        compatible = Version(package_version) in SpecifierSet(str(requirement))
    except (InvalidVersion, InvalidSpecifier, TypeError):
        compatible = False
    ensure(
        compatible,
        _INVALID_CODE,
        _INVALID_PHASE,
        "Installed PCO package is outside requires_pco",
        canonical_changed=False,
        retry_safe=False,
        value={"package_version": package_version, "requires_pco": requirement},
    )


def _ensure_unique_json_keys(value: _JsonObject, description: str) -> None:
    keys = [key for key, _ in value.pairs]
    ensure(
        len(keys) == len(set(keys)),
        _INVALID_CODE,
        _INVALID_PHASE,
        f"{description} contains duplicate JSON keys",
        canonical_changed=False,
        retry_safe=False,
    )


def _ensure_directory(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        _invalid(
            "Unable to inspect SKILL resource tree", path=str(path), value=str(exc)
        )
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        _invalid("SKILL resource tree must be a real directory", path=str(path))


def _normalize_relative(raw_path: str) -> str:
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path:
        _invalid("SKILL resource path must be a non-empty POSIX relative path", value=raw_path)
    path = PurePosixPath(raw_path)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        _invalid("SKILL resource path is unsafe", path=raw_path)
    normalized = path.as_posix()
    if normalized != raw_path:
        _invalid("SKILL resource path is not canonical", path=raw_path)
    return normalized


def _diff_hashes(expected: Mapping[str, str], observed: Mapping[str, str]) -> dict[str, Any]:
    return {
        "missing": sorted(set(expected) - set(observed)),
        "unexpected": sorted(set(observed) - set(expected)),
        "changed": {
            path: {"expected": expected[path], "observed": observed[path]}
            for path in sorted(set(expected) & set(observed))
            if expected[path] != observed[path]
        },
    }


def _invalid(message: str, **details: Any) -> None:
    raise MemError(
        _INVALID_CODE,
        _INVALID_PHASE,
        message,
        canonical_changed=False,
        retry_safe=False,
        **details,
    )
