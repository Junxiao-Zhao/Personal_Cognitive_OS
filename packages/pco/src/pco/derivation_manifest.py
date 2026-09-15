"""Strict manifests and no-follow verification for disposable derivations."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Literal
import unicodedata

from mem_core.errors import MemError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HashValue = str


def derivation_error(
    message: str,
    *,
    path: str | Path | None = None,
    value: Any = None,
    retry_safe: bool = True,
) -> MemError:
    return MemError(
        "DERIVATION_FAILED",
        "derivation",
        message,
        canonical_changed=False,
        retry_safe=retry_safe,
        path=None if path is None else str(path),
        value=value,
        recovery=["Run pco derive all to rebuild both disposable derivations"],
    )


def normalized_relative_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("derivation paths must be strings")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("derivation paths must be valid UTF-8") from exc
    candidate = PurePosixPath(value)
    if (
        not value
        or value == "."
        or "\\" in value
        or candidate.is_absolute()
        or ".." in candidate.parts
        or "." in candidate.parts
        or candidate.as_posix() != value
        or value == "manifest.json"
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("derivation paths must be normalized safe relative paths")
    return value


def platform_path_key(value: str) -> str:
    """A conservative key for path collisions on common case-folding Hosts."""

    return unicodedata.normalize("NFC", value).casefold()


class DerivationManifest(BaseModel):
    """The complete integrity control for one disposable derivation tree."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["pco_memory", "retrieval_index"]
    source_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    profile_version: str = Field(min_length=1)
    files: dict[str, str]

    @field_validator("profile_version")
    @classmethod
    def normalized_profile_version(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("profile_version must be normalized")
        return value

    @field_validator("files")
    @classmethod
    def validate_files(cls, files: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for path, digest in files.items():
            normalized_path = normalized_relative_path(path)
            if not isinstance(digest, str) or not _is_hash(digest):
                raise ValueError("derivation hashes must be sha256:<lowercase hex>")
            normalized[normalized_path] = digest
        return normalized

    @model_validator(mode="after")
    def no_platform_collisions(self) -> "DerivationManifest":
        observed: dict[str, str] = {}
        for path in self.files:
            key = platform_path_key(path)
            previous = observed.get(key)
            if previous is not None and previous != path:
                raise ValueError(
                    f"derivation paths collide on a case-folding Host: {previous!r}, {path!r}"
                )
            observed[key] = path
        return self


class _StrictObject(dict[str, Any]):
    pass


def _strict_object(pairs: list[tuple[str, Any]]) -> _StrictObject:
    result = _StrictObject()
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def strict_json_object(data: bytes, *, path: str | Path) -> dict[str, Any]:
    try:
        decoded = data.decode("utf-8", errors="strict")
        parsed = json.loads(
            decoded,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise derivation_error(
            "Derivation control JSON is malformed or ambiguous",
            path=path,
            value=str(exc),
            retry_safe=False,
        ) from exc
    if not isinstance(parsed, _StrictObject):
        raise derivation_error(
            "Derivation control JSON must contain one object",
            path=path,
            retry_safe=False,
        )
    return dict(parsed)


def canonical_manifest_bytes(manifest: DerivationManifest) -> bytes:
    return (
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def hash_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def manifest_for_files(
    files: dict[str, bytes],
    *,
    kind: Literal["pco_memory", "retrieval_index"],
    source_commit: str,
    profile_version: str,
) -> DerivationManifest:
    return DerivationManifest(
        kind=kind,
        source_commit=source_commit,
        profile_version=profile_version,
        files={path: hash_bytes(data) for path, data in sorted(files.items())},
    )


def verify_derivation_tree(
    path: Path,
    *,
    kind: Literal["pco_memory", "retrieval_index"],
    expected_commit: str | None = None,
) -> dict[str, Any]:
    """Read and verify one exact no-follow tree without filesystem mutation."""

    target = Path(path).absolute()
    try:
        observed = os.lstat(target)
    except FileNotFoundError:
        return _missing_result(kind, expected_commit)
    except OSError as exc:
        raise derivation_error(
            "Derivation tree cannot be inspected safely", path=target, value=str(exc)
        ) from exc
    if not stat.S_ISDIR(observed.st_mode):
        raise derivation_error(
            "Derivation root must be a real directory", path=target, retry_safe=False
        )

    root_identity = _identity(observed)
    snapshot, directories = _read_tree_no_follow(target, root_identity)
    if not snapshot and not directories:
        return _missing_result(kind, expected_commit)
    raw_manifest = snapshot.get("manifest.json")
    if raw_manifest is None:
        return {
            **_missing_result(kind, expected_commit),
            "additional": _utf8_sorted(
                list(snapshot) + [f"{directory}/" for directory in directories]
            ),
        }
    parsed = strict_json_object(raw_manifest, path="manifest.json")
    try:
        manifest = DerivationManifest.model_validate(parsed, strict=True)
    except Exception as exc:
        raise derivation_error(
            "Derivation manifest does not satisfy its strict schema",
            path="manifest.json",
            value=str(exc),
            retry_safe=False,
        ) from exc
    if raw_manifest != canonical_manifest_bytes(manifest):
        raise derivation_error(
            "Derivation manifest bytes are not canonical",
            path="manifest.json",
            retry_safe=False,
        )
    if manifest.kind != kind:
        raise derivation_error(
            "Derivation manifest kind does not match the requested output",
            path="manifest.json",
            value={"expected": kind, "observed": manifest.kind},
            retry_safe=False,
        )

    payload = {name: data for name, data in snapshot.items() if name != "manifest.json"}
    expected_paths = set(manifest.files)
    actual_paths = set(payload)
    missing = _utf8_sorted(expected_paths - actual_paths)
    additional_files = actual_paths - expected_paths
    expected_directories = {
        PurePosixPath(path).parent.as_posix()
        for path in expected_paths
        if PurePosixPath(path).parent.as_posix() != "."
    }
    # Include every implied ancestor, not just the immediate parent.
    for path in expected_paths:
        parent = PurePosixPath(path).parent
        while parent.as_posix() != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    additional = _utf8_sorted(
        list(additional_files)
        + [f"{directory}/" for directory in directories - expected_directories]
    )
    modified = _utf8_sorted(
        path
        for path in expected_paths & actual_paths
        if hash_bytes(payload[path]) != manifest.files[path]
    )
    stale = expected_commit is not None and manifest.source_commit != expected_commit
    ok = not missing and not modified and not additional and not stale
    return {
        "ok": ok,
        "status": "current" if ok else ("stale" if stale else "modified"),
        "kind": manifest.kind,
        "source_commit": manifest.source_commit,
        "expected_commit": expected_commit,
        "profile_version": manifest.profile_version,
        "missing": missing,
        "modified": modified,
        "additional": additional,
    }


def _missing_result(kind: str, expected_commit: str | None) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "missing",
        "kind": kind,
        "source_commit": None,
        "expected_commit": expected_commit,
        "profile_version": None,
        "missing": ["manifest.json"],
        "modified": [],
        "additional": [],
    }


def _read_tree_no_follow(
    root: Path, expected_identity: tuple[int, int]
) -> tuple[dict[str, bytes], set[str]]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise derivation_error(
            "Derivation root cannot be opened without following links",
            path=root,
            value=str(exc),
            retry_safe=False,
        ) from exc
    try:
        if _identity(os.fstat(root_fd)) != expected_identity:
            raise derivation_error(
                "Derivation root identity changed during verification", path=root
            )
        files: dict[str, bytes] = {}
        directories: set[str] = set()
        _read_directory(root_fd, PurePosixPath("."), files, directories)
        try:
            current = os.lstat(root)
        except OSError as exc:
            raise derivation_error(
                "Derivation root changed during verification",
                path=root,
                value=str(exc),
            ) from exc
        if not stat.S_ISDIR(current.st_mode) or _identity(current) != expected_identity:
            raise derivation_error(
                "Derivation root identity changed during verification", path=root
            )
        return files, directories
    finally:
        os.close(root_fd)


def _read_directory(
    directory_fd: int,
    prefix: PurePosixPath,
    files: dict[str, bytes],
    directories: set[str],
) -> None:
    try:
        names = os.listdir(directory_fd)
    except OSError as exc:
        raise derivation_error(
            "Derivation directory cannot be enumerated",
            path=prefix.as_posix(),
            value=str(exc),
        ) from exc
    normalized_names: list[str] = []
    for name in names:
        try:
            name.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise derivation_error(
                "Derivation entry name is not valid UTF-8",
                path=prefix.as_posix(),
                retry_safe=False,
            ) from exc
        if name in {".", ".."} or "/" in name or "\\" in name:
            raise derivation_error(
                "Derivation entry name is unsafe", path=name, retry_safe=False
            )
        normalized_names.append(name)
    for name in _utf8_sorted(normalized_names):
        relative = (prefix / name).as_posix()
        if relative.startswith("./"):
            relative = relative[2:]
        try:
            named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise derivation_error(
                "Derivation entry changed during verification",
                path=relative,
                value=str(exc),
            ) from exc
        if stat.S_ISLNK(named.st_mode):
            raise derivation_error(
                "Derivation tree contains a symlink", path=relative, retry_safe=False
            )
        identity = (named.st_dev, named.st_ino)
        if stat.S_ISDIR(named.st_mode):
            try:
                child_fd = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory_fd,
                )
            except OSError as exc:
                raise derivation_error(
                    "Derivation directory changed during verification",
                    path=relative,
                    value=str(exc),
                ) from exc
            try:
                if _identity(os.fstat(child_fd)) != identity:
                    raise derivation_error(
                        "Derivation directory identity changed during verification",
                        path=relative,
                    )
                directories.add(relative)
                _read_directory(child_fd, PurePosixPath(relative), files, directories)
                _require_named_identity(directory_fd, name, identity, relative)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(named.st_mode):
            files[relative] = _read_regular(directory_fd, name, identity, relative)
        else:
            raise derivation_error(
                "Derivation tree contains a non-regular entry",
                path=relative,
                retry_safe=False,
            )
    try:
        final_names = os.listdir(directory_fd)
    except OSError as exc:
        raise derivation_error(
            "Derivation directory changed during verification",
            path=prefix.as_posix(),
            value=str(exc),
        ) from exc
    if set(final_names) != set(names):
        raise derivation_error(
            "Derivation directory contents changed during verification",
            path=prefix.as_posix(),
        )


def _read_regular(
    parent_fd: int, name: str, identity: tuple[int, int], relative: str
) -> bytes:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise derivation_error(
            "Derivation file changed during verification", path=relative, value=str(exc)
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or _identity(before) != identity:
            raise derivation_error(
                "Derivation file identity changed during verification", path=relative
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if _file_version(before) != _file_version(after):
            raise derivation_error(
                "Derivation file changed while it was read", path=relative
            )
        _require_named_identity(parent_fd, name, identity, relative)
        return b"".join(chunks)
    except OSError as exc:
        raise derivation_error(
            "Derivation file cannot be read safely", path=relative, value=str(exc)
        ) from exc
    finally:
        os.close(descriptor)


def _require_named_identity(
    parent_fd: int, name: str, identity: tuple[int, int], relative: str
) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise derivation_error(
            "Derivation entry changed during verification",
            path=relative,
            value=str(exc),
        ) from exc
    if _identity(current) != identity:
        raise derivation_error(
            "Derivation entry identity changed during verification", path=relative
        )


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _file_version(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _utf8_sorted(values: Any) -> list[str]:
    return sorted(values, key=lambda value: value.encode("utf-8"))


def _is_hash(value: str) -> bool:
    return (
        len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )
