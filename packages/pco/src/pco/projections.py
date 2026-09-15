"""Pure construction of the v0.5 current-state ``PCO_MEMORY`` projection."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from typing import Any
from urllib.parse import quote

from mem_core.errors import MemError
from mem_core.profile import ProfileConfig, load_profile_yaml
from mem_core.repository import MemoryRepository

from .config import PROFILE_VERSION
from .derivation_manifest import (
    DerivationManifest,
    canonical_manifest_bytes,
    derivation_error,
    manifest_for_files,
    platform_path_key,
    strict_json_object,
)
from .proposals.markdown import render_business_markdown
from .validation import CANONICAL_BUSINESS_STREAMS, latest_effective

_DIRECTORY_STREAMS = (
    "sources",
    "evidence",
    "events",
    "psychologies",
    "philosophies",
    "archetypes",
    "hypotheses",
)
_REQUIRED_STREAMS = frozenset(
    (*_DIRECTORY_STREAMS, "meta_revisions", "session_handoffs")
)
_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_V05_STREAMS = {
    "evidence": (
        "evidence/evidence.jsonl",
        "schemas/evidence.schema.json",
        "pco/evidence/v1",
    ),
    "sources": ("sources/sources.jsonl", "schemas/source.schema.json", "pco/source/v1"),
    "events": ("events/events.jsonl", "schemas/event.schema.json", "pco/event/v1"),
    "psychologies": (
        "psychologies/psychologies.jsonl",
        "schemas/concept.schema.json",
        "pco/psychology/v1",
    ),
    "philosophies": (
        "philosophies/philosophies.jsonl",
        "schemas/concept.schema.json",
        "pco/philosophy/v1",
    ),
    "archetypes": (
        "archetypes/archetypes.jsonl",
        "schemas/archetype.schema.json",
        "pco/archetype/v1",
    ),
    "hypotheses": (
        "hypotheses/hypotheses.jsonl",
        "schemas/hypothesis.schema.json",
        "pco/hypothesis/v1",
    ),
    "meta_revisions": (
        "meta_revisions/meta_revisions.jsonl",
        "schemas/meta-revision.schema.json",
        "pco/meta-revision/v1",
    ),
    "session_handoffs": (
        "session_handoffs/session_handoffs.jsonl",
        "schemas/session-handoff.schema.json",
        "pco/session-handoff/v1",
    ),
}


def build_projection(
    repository: MemoryRepository, target: Path, source_commit: str
) -> DerivationManifest:
    """Build one new projection from exact Git-object bytes at ``source_commit``."""

    files, profile_version = _projection_payloads(repository, source_commit)
    manifest = manifest_for_files(
        files,
        kind="pco_memory",
        source_commit=source_commit,
        profile_version=profile_version,
    )
    _write_new_tree(
        Path(target), {**files, "manifest.json": canonical_manifest_bytes(manifest)}
    )
    return manifest


def projection_path(stream: str, record_id: str) -> str:
    """Map a Profile-valid record ID to one deterministic portable path."""

    if not isinstance(record_id, str) or not record_id:
        raise derivation_error(
            "Canonical record ID cannot be mapped to a projection path",
            value=record_id,
            retry_safe=False,
        )
    try:
        encoded = record_id.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise derivation_error(
            "Canonical record ID is not valid UTF-8",
            value=record_id,
            retry_safe=False,
        ) from exc
    if stream == "meta_revisions":
        if record_id != "meta_current":
            raise derivation_error(
                "Meta-memory must use the canonical meta_current ID",
                value=record_id,
                retry_safe=False,
            )
        return "META_MEMORY.md"
    if stream == "session_handoffs":
        return "SESSION_HANDOFF.md"
    if stream not in _DIRECTORY_STREAMS:
        raise derivation_error(
            "Canonical record uses an unknown projection stream",
            value=stream,
            retry_safe=False,
        )
    reserved = record_id.split(".", 1)[0].upper() in _WINDOWS_RESERVED
    if _PORTABLE_ID.fullmatch(record_id) and not reserved:
        name = record_id
    else:
        name = quote(encoded, safe="")
        if reserved or len(name.encode("ascii")) > 180:
            name = "~" + hashlib.sha256(encoded).hexdigest()
    return f"{stream}/{name}.md"


def _projection_payloads(
    repository: MemoryRepository, source_commit: str
) -> tuple[dict[str, bytes], str]:
    profile = _exact_profile(repository, source_commit)
    records: dict[str, list[dict[str, Any]]] = {}
    for stream in sorted(_REQUIRED_STREAMS, key=str.encode):
        config = profile.streams[stream]
        records[stream] = _parse_jsonl(
            _git_blob(repository, source_commit, config.path), config.path
        )

    files: dict[str, bytes] = {}
    origins: dict[str, tuple[str, str]] = {}
    collision_keys: dict[str, str] = {}
    for stream in _DIRECTORY_STREAMS:
        current = latest_effective(records[stream])
        for record_id in sorted(current, key=str.encode):
            relative = projection_path(stream, record_id)
            _reserve_path(relative, stream, record_id, origins, collision_keys)
            files[relative] = _render(current[record_id], relative)

    meta = latest_effective(records["meta_revisions"]).get("meta_current")
    if meta is not None:
        relative = projection_path("meta_revisions", "meta_current")
        _reserve_path(
            relative, "meta_revisions", "meta_current", origins, collision_keys
        )
        files[relative] = _render(meta, relative)

    handoffs = latest_effective(records["session_handoffs"])
    if handoffs:
        selected = max(handoffs.values(), key=_handoff_key)
        relative = projection_path("session_handoffs", selected["id"])
        _reserve_path(
            relative, "session_handoffs", selected["id"], origins, collision_keys
        )
        files[relative] = _render(selected, relative)
    return dict(sorted(files.items())), profile.version


def exact_canonical_records(
    repository: MemoryRepository,
    source_commit: str,
    *,
    canonical_head: str | None = None,
) -> tuple[ProfileConfig, dict[str, list[dict[str, Any]]]]:
    """Read every v0.5 business stream from one verified canonical commit."""

    profile = _exact_profile(repository, source_commit, canonical_head=canonical_head)
    records = {
        stream: _parse_jsonl(
            _git_blob(repository, source_commit, profile.streams[stream].path),
            profile.streams[stream].path,
        )
        for stream in sorted(_REQUIRED_STREAMS, key=str.encode)
    }
    return profile, records


def _render(record: Any, relative: str) -> bytes:
    try:
        return render_business_markdown(dict(record))
    except (MemError, TypeError, ValueError) as exc:
        value = exc.as_dict() if isinstance(exc, MemError) else str(exc)
        raise derivation_error(
            "Canonical record cannot be rendered as v0.5 business Markdown",
            path=relative,
            value=value,
            retry_safe=False,
        ) from exc


def _reserve_path(
    relative: str,
    stream: str,
    record_id: str,
    origins: dict[str, tuple[str, str]],
    collision_keys: dict[str, str],
) -> None:
    if relative in origins:
        raise derivation_error(
            "Two canonical records map to one projection path",
            path=relative,
            value={"first": origins[relative], "second": (stream, record_id)},
            retry_safe=False,
        )
    key = platform_path_key(relative)
    previous = collision_keys.get(key)
    if previous is not None and previous != relative:
        raise derivation_error(
            "Canonical record paths collide on a case-folding Host",
            path=relative,
            value={"first": previous, "second": relative},
            retry_safe=False,
        )
    origins[relative] = (stream, record_id)
    collision_keys[key] = relative


def _handoff_key(record: Any) -> tuple[str, int, bytes]:
    recorded_at = record.get("recorded_at")
    revision = record.get("revision")
    record_id = record.get("id")
    return (
        recorded_at if isinstance(recorded_at, str) else "",
        (
            revision
            if isinstance(revision, int) and not isinstance(revision, bool)
            else -1
        ),
        record_id.encode("utf-8") if isinstance(record_id, str) else b"",
    )


def _exact_profile(
    repository: MemoryRepository,
    source_commit: str,
    *,
    canonical_head: str | None = None,
) -> ProfileConfig:
    if not isinstance(source_commit, str) or not _COMMIT.fullmatch(source_commit):
        raise derivation_error(
            "source_commit must be one full lowercase hexadecimal commit ID",
            value=source_commit,
            retry_safe=False,
        )
    result = _git_probe(
        repository, "rev-parse", "--verify", f"{source_commit}^{{commit}}"
    )
    observed_commit = result.stdout.decode("ascii", errors="replace").strip()
    if result.returncode or observed_commit != source_commit:
        raise derivation_error(
            "source_commit does not identify an exact canonical commit",
            value=source_commit,
            retry_safe=False,
        )
    if canonical_head is None:
        head_result = _git_probe(repository, "rev-parse", "--verify", "HEAD^{commit}")
        head_commit = head_result.stdout.decode("ascii", errors="replace").strip()
        if (
            head_result.returncode
            or not _COMMIT.fullmatch(head_commit)
            or len(head_commit) != len(source_commit)
        ):
            raise derivation_error(
                "Canonical HEAD cannot be resolved to one full commit ID",
                value=head_result.stderr.decode("utf-8", errors="replace").strip(),
                retry_safe=False,
            )
    else:
        head_commit = canonical_head
        if (
            not isinstance(head_commit, str)
            or not _COMMIT.fullmatch(head_commit)
            or len(head_commit) != len(source_commit)
        ):
            raise derivation_error(
                "Captured canonical HEAD is not one full commit ID",
                value=head_commit,
                retry_safe=False,
            )
    if source_commit != head_commit:
        ancestry = _git_probe(
            repository,
            "merge-base",
            "--is-ancestor",
            source_commit,
            head_commit,
        )
        if ancestry.returncode == 1:
            raise derivation_error(
                "source_commit is not in canonical HEAD history",
                value={"source_commit": source_commit, "head": head_commit},
                retry_safe=False,
            )
        if ancestry.returncode != 0:
            raise derivation_error(
                "Canonical commit ancestry cannot be established",
                value=ancestry.stderr.decode("utf-8", errors="replace").strip(),
                retry_safe=False,
            )
    marker = strict_json_object(
        _git_blob(repository, source_commit, ".mem-profile.json"),
        path=".mem-profile.json",
    )
    profile_path = marker.get("path")
    if not isinstance(profile_path, str):
        raise derivation_error(
            "Canonical Profile marker has no safe Profile path",
            path=".mem-profile.json",
            retry_safe=False,
        )
    pure_profile = PurePosixPath(profile_path)
    if (
        not profile_path
        or pure_profile.is_absolute()
        or ".." in pure_profile.parts
        or pure_profile.as_posix() != profile_path
    ):
        raise derivation_error(
            "Canonical Profile marker path is unsafe",
            path=".mem-profile.json",
            retry_safe=False,
        )
    profile_location = f"{profile_path}/profile.yaml"
    try:
        loaded = load_profile_yaml(
            _git_blob(repository, source_commit, profile_location).decode(
                "utf-8", errors="strict"
            )
        )
        profile = ProfileConfig.model_validate(loaded, strict=True)
    except (UnicodeDecodeError, TypeError, ValueError) as exc:
        raise derivation_error(
            "Canonical exact-commit Profile is invalid",
            path=profile_location,
            value=str(exc),
            retry_safe=False,
        ) from exc
    exact_streams = {
        name: (item.path, item.schema_path, item.schema_version)
        for name, item in profile.streams.items()
    }
    if (
        profile_path != "profiles/pco"
        or marker.get("name") != profile.name
        or marker.get("version") != profile.version
        or profile.name != "pco"
        or profile.version != PROFILE_VERSION
        or profile.application_commit_policy != "pco_proposal"
        or exact_streams != _V05_STREAMS
        or set(profile.streams) != _REQUIRED_STREAMS
        or set(CANONICAL_BUSINESS_STREAMS) != _REQUIRED_STREAMS
    ):
        raise derivation_error(
            "source_commit Profile does not match the opened v0.5 repository",
            path=".mem-profile.json",
            value={
                "marker": {
                    "name": marker.get("name"),
                    "version": marker.get("version"),
                },
                "exact": {"name": profile.name, "version": profile.version},
                "required": {"name": "pco", "version": PROFILE_VERSION},
            },
            retry_safe=False,
        )
    return profile


def _git_probe(
    repository: MemoryRepository, *arguments: str
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(repository.root), *arguments],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise derivation_error(
            "Canonical Git boundary cannot be inspected",
            value=str(exc),
            retry_safe=False,
        ) from exc


def _git_blob(repository: MemoryRepository, commit: str, relative: str) -> bytes:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repository.root),
                "cat-file",
                "blob",
                f"{commit}:{relative}",
            ],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise derivation_error(
            "Canonical Git object cannot be read", path=relative, value=str(exc)
        ) from exc
    if result.returncode:
        raise derivation_error(
            "Canonical Git object is missing",
            path=relative,
            value=result.stderr.decode("utf-8", errors="replace").strip(),
            retry_safe=False,
        )
    return result.stdout


def _parse_jsonl(data: bytes, relative: str) -> list[dict[str, Any]]:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise derivation_error(
            "Canonical stream is not UTF-8", path=relative, retry_safe=False
        ) from exc
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        record = strict_json_object(
            line.encode("utf-8"), path=f"{relative}:{line_number}"
        )
        records.append(record)
    return records


@dataclass(frozen=True)
class _WrittenTreeIdentity:
    target: Path
    parent: Path
    name: str
    parent_identity: tuple[int, int]
    target_identity: tuple[int, int]


def _write_new_tree(target: Path, files: dict[str, bytes]) -> _WrittenTreeIdentity:
    target = Path(target)
    name = target.name
    try:
        name.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise derivation_error(
            "Projection target name must be valid UTF-8",
            path=target,
            retry_safe=False,
        ) from exc
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(character) < 32 for character in name)
    ):
        raise derivation_error(
            "Projection target must have one safe final path component",
            path=target,
            retry_safe=False,
        )
    target = target.absolute()
    parent_path = target.parent
    try:
        parent_observed = os.lstat(parent_path)
    except (OSError, TypeError, ValueError) as exc:
        raise derivation_error(
            "Projection target parent cannot be inspected safely",
            path=parent_path,
            value=str(exc),
            retry_safe=False,
        ) from exc
    if not stat.S_ISDIR(parent_observed.st_mode):
        raise derivation_error(
            "Projection target parent must be a real directory",
            path=parent_path,
            retry_safe=False,
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        parent_fd = os.open(parent_path, flags)
    except (OSError, TypeError, ValueError) as exc:
        raise derivation_error(
            "Projection target parent cannot be opened without following links",
            path=parent_path,
            value=str(exc),
            retry_safe=False,
        ) from exc
    root_fd: int | None = None
    try:
        parent_identity = (parent_observed.st_dev, parent_observed.st_ino)
        if _stat_identity(os.fstat(parent_fd)) != parent_identity:
            raise derivation_error(
                "Projection target parent identity changed while it was opened",
                path=parent_path,
                retry_safe=False,
            )
        _require_path_identity(parent_path, parent_identity)
        try:
            observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            _require_path_identity(parent_path, parent_identity)
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            except (OSError, TypeError, ValueError) as exc:
                raise derivation_error(
                    "Projection target cannot be created safely",
                    path=target,
                    value=str(exc),
                    retry_safe=False,
                ) from exc
            try:
                observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except (OSError, TypeError, ValueError) as exc:
                raise derivation_error(
                    "Projection target changed after creation",
                    path=target,
                    value=str(exc),
                    retry_safe=False,
                ) from exc
        except (OSError, TypeError, ValueError) as exc:
            raise derivation_error(
                "Projection target cannot be inspected without following links",
                path=target,
                value=str(exc),
                retry_safe=False,
            ) from exc
        if not stat.S_ISDIR(observed.st_mode):
            raise derivation_error(
                "Projection target must be absent or an empty real directory",
                path=target,
                retry_safe=False,
            )
        target_identity = _stat_identity(observed)
        _require_named_identity(parent_fd, name, target_identity, target)
        _require_path_identity(parent_path, parent_identity)
        try:
            root_fd = os.open(name, flags, dir_fd=parent_fd)
        except (OSError, TypeError, ValueError) as exc:
            raise derivation_error(
                "Projection target changed while it was opened",
                path=target,
                value=str(exc),
                retry_safe=False,
            ) from exc
        if _stat_identity(os.fstat(root_fd)) != target_identity:
            raise derivation_error(
                "Projection target identity changed while it was opened",
                path=target,
                retry_safe=False,
            )
        _require_named_identity(parent_fd, name, target_identity, target)
        _require_path_identity(parent_path, parent_identity)
        try:
            entries = os.listdir(root_fd)
        except OSError as exc:
            raise derivation_error(
                "Projection target cannot be enumerated safely",
                path=target,
                value=str(exc),
                retry_safe=False,
            ) from exc
        if entries:
            raise derivation_error(
                "Projection target must be empty", path=target, retry_safe=False
            )
        for relative, content in sorted(
            files.items(), key=lambda item: item[0].encode("utf-8")
        ):
            _write_exclusive(root_fd, relative, content)
        _require_named_identity(parent_fd, name, target_identity, target)
        _require_path_identity(parent_path, parent_identity)
        return _WrittenTreeIdentity(
            target=target,
            parent=parent_path,
            name=name,
            parent_identity=parent_identity,
            target_identity=target_identity,
        )
    finally:
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)


def _stat_identity(observed: os.stat_result) -> tuple[int, int]:
    return observed.st_dev, observed.st_ino


def _require_path_identity(path: Path, identity: tuple[int, int]) -> None:
    try:
        observed = os.lstat(path)
    except (OSError, TypeError, ValueError) as exc:
        raise derivation_error(
            "Projection target parent changed during construction",
            path=path,
            value=str(exc),
            retry_safe=False,
        ) from exc
    if not stat.S_ISDIR(observed.st_mode) or _stat_identity(observed) != identity:
        raise derivation_error(
            "Projection target parent identity changed during construction",
            path=path,
            retry_safe=False,
        )


def _require_named_identity(
    parent_fd: int,
    name: str,
    identity: tuple[int, int],
    display_path: Path,
) -> None:
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except (OSError, TypeError, ValueError) as exc:
        raise derivation_error(
            "Projection target name changed during construction",
            path=display_path,
            value=str(exc),
            retry_safe=False,
        ) from exc
    if not stat.S_ISDIR(observed.st_mode) or _stat_identity(observed) != identity:
        raise derivation_error(
            "Projection target identity changed during construction",
            path=display_path,
            retry_safe=False,
        )


def _write_exclusive(root_fd: int, relative: str, content: bytes) -> None:
    parts = PurePosixPath(relative).parts
    current = os.dup(root_fd)
    descriptor: int | None = None
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=current)
            except FileExistsError:
                pass
            following = os.open(
                part,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=current,
            )
            os.close(current)
            current = following
        descriptor = os.open(
            parts[-1],
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=current,
        )
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short projection write")
            view = view[written:]
    except (OSError, ValueError) as exc:
        raise derivation_error(
            "Projection payload cannot be written without replacement",
            path=relative,
            value=str(exc),
            retry_safe=False,
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(current)
