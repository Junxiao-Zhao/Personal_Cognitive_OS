"""Descriptor-anchored capture of one closed, immutable proposal tree."""

from __future__ import annotations

import codecs
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any

from mem_core.errors import MemError
from pydantic import TypeAdapter, ValidationError

from .materials import (
    MAX_INPUT_MATERIAL_BYTES,
    CapturedMaterial,
    _local_file_path,
    _require_read_capabilities,
    digest,
)
from .models import InputMaterial, LocalFileMaterial, ProposalArtifactMaterial, ProposalManifest


MAX_IMMUTABLE_SNAPSHOT_BYTES = 67_108_864
_READ_CHUNK_BYTES = 1024 * 1024
_INPUT_ADAPTER = TypeAdapter(InputMaterial)
_LEGAL_TOP_DIRECTORIES = frozenset(
    {
        "evidence",
        "sources",
        "events",
        "psychologies",
        "philosophies",
        "archetypes",
        "hypotheses",
        "inputs",
    }
)


@dataclass(frozen=True, slots=True)
class TreeEntry:
    path: PurePosixPath
    kind: str
    identity: tuple[int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class CapturedBusinessFile:
    path: PurePosixPath
    operation: str
    data: bytes
    file_hash: str
    identity: tuple[int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class _MaterialValue:
    descriptor_json: bytes
    raw_bytes: bytes
    normalized_bytes: bytes
    content_hash: str
    file_hash: str | None
    identity: tuple[int, int, int, int]

    def public(self) -> CapturedMaterial:
        return CapturedMaterial(
            material=_INPUT_ADAPTER.validate_python(json.loads(self.descriptor_json)),
            raw_bytes=self.raw_bytes,
            normalized_bytes=self.normalized_bytes,
            content_hash=self.content_hash,
            file_hash=self.file_hash,
            identity=self.identity,
        )


@dataclass(frozen=True, slots=True)
class ProposalSnapshot:
    """A deep immutable value; model-valued properties return detached copies."""

    manifest_bytes: bytes
    business_files: tuple[CapturedBusinessFile, ...]
    _input_material: tuple[_MaterialValue, ...]
    tree_entries: tuple[TreeEntry, ...]
    total_bytes: int

    @property
    def manifest(self) -> ProposalManifest:
        return ProposalManifest.model_validate(json.loads(self.manifest_bytes))

    @property
    def input_material(self) -> tuple[CapturedMaterial, ...]:
        return tuple(item.public() for item in self._input_material)


def _error(
    code: str,
    message: str,
    *,
    path: Path | PurePosixPath | None = None,
    value: Any = None,
) -> MemError:
    return MemError(
        code,
        "proposal_snapshot",
        message,
        path=None if path is None else str(path),
        value=value,
        recovery=["Restore the proposal to the exact closed manifest set and retry"],
        canonical_changed=False,
        retry_safe=False,
    )


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        stat.S_IFMT(info.st_mode),
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _open_root(root: Path) -> tuple[int, tuple[int, int, int, int, int, int]]:
    required = (
        os.name == "posix"
        and getattr(os, "O_NOFOLLOW", None) is not None
        and getattr(os, "O_NONBLOCK", None) is not None
        and getattr(os, "O_DIRECTORY", None) is not None
        and os.open in getattr(os, "supports_dir_fd", set())
        and os.stat in getattr(os, "supports_dir_fd", set())
        and os.stat in getattr(os, "supports_follow_symlinks", set())
    )
    if not required:
        raise _error("PROPOSAL_PATH_UNSAFE", "Safe proposal traversal is unavailable", path=root)
    try:
        observed = os.stat(root, follow_symlinks=False)
        descriptor = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise _error("PROPOSAL_PATH_UNSAFE", "Proposal root cannot be opened safely", path=root, value=str(exc)) from exc
    try:
        try:
            anchored = os.fstat(descriptor)
        except OSError as exc:
            raise _error(
                "PROPOSAL_PATH_UNSAFE",
                "Proposal root identity cannot be inspected",
                path=root,
                value=str(exc),
            ) from exc
        if not stat.S_ISDIR(observed.st_mode) or _identity(observed)[:3] != _identity(anchored)[:3]:
            raise _error("PROPOSAL_PATH_UNSAFE", "Proposal root changed while opening", path=root)
        return descriptor, _identity(anchored)
    except BaseException:
        os.close(descriptor)
        raise


def _named_stat(parent_fd: int, name: str, display: Path) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise _error("PROPOSAL_CHANGED_DURING_SNAPSHOT", "Proposal entry disappeared during capture", path=display, value=str(exc)) from exc


def _open_directory(parent_fd: int, name: str, display: Path) -> int:
    observed = _named_stat(parent_fd, name, display)
    if not stat.S_ISDIR(observed.st_mode):
        raise _error("PROPOSAL_PATH_UNSAFE", "Proposal path component is not a real directory", path=display)
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise _error("PROPOSAL_PATH_UNSAFE", "Proposal directory cannot be opened without following links", path=display, value=str(exc)) from exc
    try:
        try:
            anchored = os.fstat(descriptor)
        except OSError as exc:
            raise _error(
                "PROPOSAL_PATH_UNSAFE",
                "Proposal directory identity cannot be inspected",
                path=display,
                value=str(exc),
            ) from exc
        if _identity(observed)[:3] != _identity(anchored)[:3]:
            raise _error("PROPOSAL_CHANGED_DURING_SNAPSHOT", "Proposal directory changed while opening", path=display)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_parent(root_fd: int, root: Path, path: PurePosixPath) -> tuple[list[int], int]:
    anchors: list[int] = []
    current = root_fd
    display = root
    try:
        for part in path.parts[:-1]:
            display /= part
            current = _open_directory(current, part, display)
            anchors.append(current)
        return anchors, current
    except BaseException:
        for descriptor in reversed(anchors):
            os.close(descriptor)
        raise


def _read_exact(
    root_fd: int,
    root: Path,
    path: PurePosixPath,
    *,
    limit: int,
    aggregate_remaining: int | None = None,
) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
    anchors, parent = _open_parent(root_fd, root, path)
    display = root / Path(*path.parts)
    descriptor: int | None = None
    try:
        observed = _named_stat(parent, path.name, display)
        if not stat.S_ISREG(observed.st_mode):
            raise _error("PROPOSAL_PATH_UNSAFE", "Proposal file must be a no-follow regular file", path=display)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(path.name, flags, dir_fd=parent)
        except OSError as exc:
            raise _error("PROPOSAL_PATH_UNSAFE", "Proposal file cannot be opened safely", path=display, value=str(exc)) from exc
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or _identity(observed)[:3] != _identity(before)[:3]:
            raise _error("PROPOSAL_CHANGED_DURING_SNAPSHOT", "Proposal file changed while opening", path=display)
        if before.st_size > limit:
            raise _error("INPUT_MATERIAL_TOO_LARGE", "Proposal file exceeds its individual limit", path=display, value={"limit": limit})
        data = bytearray()
        while True:
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, max(1, limit - len(data) + 1)))
            if not chunk:
                break
            if len(data) + len(chunk) > limit:
                raise _error("INPUT_MATERIAL_TOO_LARGE", "Proposal file exceeds its individual limit", path=display, value={"limit": limit})
            if aggregate_remaining is not None and len(data) + len(chunk) > aggregate_remaining:
                raise _error(
                    "PROPOSAL_SNAPSHOT_TOO_LARGE",
                    "Immutable proposal snapshot exceeds the aggregate byte limit while reading a proposal file",
                    path=display,
                    value={"limit": MAX_IMMUTABLE_SNAPSHOT_BYTES},
                )
            data.extend(chunk)
        after = os.fstat(descriptor)
        if _identity(after) != _identity(before):
            raise _error("PROPOSAL_CHANGED_DURING_SNAPSHOT", "Proposal file changed while being read", path=display)
        return bytes(data), _identity(before)
    except OSError as exc:
        raise _error("PROPOSAL_PATH_UNSAFE", "Proposal file could not be inspected or read", path=display, value=str(exc)) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for anchor in reversed(anchors):
            os.close(anchor)


def _enumerate_tree(root_fd: int, root: Path) -> tuple[TreeEntry, ...]:
    entries: list[TreeEntry] = []

    def walk(directory_fd: int, prefix: PurePosixPath) -> None:
        try:
            with os.scandir(directory_fd) as children:
                observed = sorted(
                    children, key=lambda item: item.name.encode("utf-8")
                )
        except UnicodeError as exc:
            raise _error(
                "PROPOSAL_PATH_UNSAFE",
                "Proposal entry names must be valid UTF-8",
                path=root / Path(*prefix.parts),
                value=str(exc),
            ) from exc
        except OSError as exc:
            raise _error("PROPOSAL_PATH_UNSAFE", "Proposal directory cannot be enumerated safely", path=root / Path(*prefix.parts), value=str(exc)) from exc
        for child in observed:
            relative = PurePosixPath(child.name) if str(prefix) == "." else prefix / child.name
            display = root / Path(*relative.parts)
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise _error("PROPOSAL_CHANGED_DURING_SNAPSHOT", "Proposal entry changed during enumeration", path=display, value=str(exc)) from exc
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise _error("PROPOSAL_PATH_UNSAFE", "Proposal tree contains a symlink or special file", path=display)
            kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
            entries.append(TreeEntry(relative, kind, _identity(info)))
            if kind == "directory":
                child_fd = _open_directory(directory_fd, child.name, display)
                try:
                    walk(child_fd, relative)
                finally:
                    os.close(child_fd)

    walk(root_fd, PurePosixPath("."))
    entries.sort(key=lambda item: item.path.as_posix().encode("utf-8"))
    return tuple(entries)


def _validate_closed_set(entries: tuple[TreeEntry, ...], manifest: ProposalManifest) -> None:
    expected_files = {PurePosixPath("manifest.json")}
    expected_files.update(item.path for item in manifest.files)
    expected_files.update(
        item.path for item in manifest.input_material if isinstance(item, ProposalArtifactMaterial)
    )
    actual_files = {item.path for item in entries if item.kind == "file"}
    if actual_files != expected_files:
        raise _error(
            "PROPOSAL_MANIFEST_MISMATCH",
            "Proposal files do not equal the manifest closed set",
            value={
                "missing": sorted((str(path) for path in expected_files - actual_files), key=lambda value: value.encode("utf-8")),
                "unlisted": sorted((str(path) for path in actual_files - expected_files), key=lambda value: value.encode("utf-8")),
            },
        )
    required_directories: set[PurePosixPath] = set()
    for path in expected_files:
        required_directories.update(parent for parent in path.parents if str(parent) != ".")
    for entry in entries:
        if entry.kind != "directory":
            continue
        path = entry.path
        legal_empty_top = len(path.parts) == 1 and path.name in _LEGAL_TOP_DIRECTORIES
        if path not in required_directories and not legal_empty_top:
            raise _error("PROPOSAL_MANIFEST_MISMATCH", "Proposal tree contains an illegal directory", path=path)
        if len(path.parts) > 1 and path.parts[0] != "inputs":
            raise _error("PROPOSAL_MANIFEST_MISMATCH", "Business directories cannot be nested", path=path)


def _parse_manifest(data: bytes, root: Path) -> ProposalManifest:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate manifest key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=unique_object)
        manifest = ProposalManifest.model_validate(value)
        manifest.review_id.encode("utf-8")
        for item in manifest.files:
            item.path.as_posix().encode("utf-8")
        for item in manifest.input_material:
            item.material_id.encode("utf-8")
            if isinstance(item, ProposalArtifactMaterial):
                item.path.as_posix().encode("utf-8")
        return manifest
    except MemError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        raise _error("PROPOSAL_MANIFEST_MISMATCH", "manifest.json is not a valid proposal manifest", path=root / "manifest.json", value=str(exc)) from exc


def _budget(total: int, addition: int) -> int:
    if addition < 0 or total > MAX_IMMUTABLE_SNAPSHOT_BYTES - addition:
        raise _error(
            "PROPOSAL_SNAPSHOT_TOO_LARGE",
            "Immutable proposal snapshot exceeds the aggregate byte limit",
            value={"limit": MAX_IMMUTABLE_SNAPSHOT_BYTES, "observed_at_least": total + addition},
        )
    return total + addition


def _normalize_snapshot_chunks(
    chunks: Any,
    *,
    aggregate_remaining: int,
    path: Path,
) -> tuple[bytes, bytes]:
    """Normalize while bounding raw, normalized, and aggregate retained bytes."""

    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    raw = bytearray()
    normalized = bytearray()
    prefix = bytearray()
    started = False
    pending_cr = False
    unsupported = (b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00", b"\xfe\xff", b"\xff\xfe")

    def too_large(form: str) -> None:
        raise _error(
            "INPUT_MATERIAL_TOO_LARGE",
            f"Input material {form} bytes exceed the individual limit",
            path=path,
            value={"form": form, "limit": MAX_INPUT_MATERIAL_BYTES},
        )

    def extend(text: str) -> None:
        nonlocal pending_cr
        if "\x00" in text:
            raise _error("INPUT_MATERIAL_INVALID", "Input material contains a NUL code point", path=path)
        if pending_cr:
            if text.startswith("\n"):
                text = text[1:]
            text = "\n" + text
            pending_cr = False
        if text.endswith("\r"):
            text = text[:-1]
            pending_cr = True
        encoded = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        new_size = len(normalized) + len(encoded)
        if new_size > MAX_INPUT_MATERIAL_BYTES:
            too_large("normalized")
        if new_size > aggregate_remaining:
            raise _error(
                "PROPOSAL_SNAPSHOT_TOO_LARGE",
                "Immutable proposal snapshot exceeds the aggregate byte limit while normalizing material",
                path=path,
                value={"limit": MAX_IMMUTABLE_SNAPSHOT_BYTES},
            )
        normalized.extend(encoded)

    def reject_bom(data: bytes) -> None:
        if any(data.startswith(mark) for mark in unsupported):
            raise _error("INPUT_MATERIAL_INVALID", "Input material begins with a UTF-16 or UTF-32 byte-order mark", path=path)

    def decode(data: bytes) -> None:
        text = decoder.decode(data, final=False)
        if text:
            extend(text)

    try:
        for chunk in chunks:
            if len(raw) + len(chunk) > MAX_INPUT_MATERIAL_BYTES:
                too_large("raw")
            raw.extend(chunk)
            if not started:
                prefix_size = min(4 - len(prefix), len(chunk))
                prefix.extend(chunk[:prefix_size])
                if len(prefix) < 4:
                    continue
                reject_bom(bytes(prefix))
                started = True
                decode(bytes(prefix))
                prefix.clear()
                decode(chunk[prefix_size:])
            else:
                decode(chunk)
        if not started:
            reject_bom(bytes(prefix))
            decode(bytes(prefix))
        final = decoder.decode(b"", final=True)
        if final:
            extend(final)
    except UnicodeDecodeError as exc:
        raise _error("INPUT_MATERIAL_INVALID", "Input material is not strict UTF-8 text", path=path, value=str(exc)) from exc
    if pending_cr:
        pending_cr = False
        if len(normalized) + 1 > MAX_INPUT_MATERIAL_BYTES:
            too_large("normalized")
        if len(normalized) + 1 > aggregate_remaining:
            raise _error(
                "PROPOSAL_SNAPSHOT_TOO_LARGE",
                "Immutable proposal snapshot exceeds the aggregate byte limit while normalizing material",
                path=path,
                value={"limit": MAX_IMMUTABLE_SNAPSHOT_BYTES},
            )
        normalized.extend(b"\n")
    if normalized and not normalized.endswith(b"\n"):
        extend("\n")
    return bytes(raw), bytes(normalized)


def _capture_local_material(
    material: LocalFileMaterial, *, aggregate_remaining: int
) -> CapturedMaterial:
    path = _local_file_path(material.locator)
    _require_read_capabilities(anchored=False, path=path)
    try:
        observed = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise _error("INPUT_MATERIAL_UNSAFE", "Local material cannot be inspected safely", path=path, value=str(exc)) from exc
    if not stat.S_ISREG(observed.st_mode):
        raise _error("INPUT_MATERIAL_UNSAFE", "Local material must be a no-follow regular file", path=path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise _error("INPUT_MATERIAL_UNSAFE", "Local material cannot be opened safely", path=path, value=str(exc)) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or _identity(observed)[:3] != _identity(before)[:3]:
            raise _error("INPUT_MATERIAL_UNSAFE", "Local material changed while opening", path=path)
        if before.st_size < 0:
            raise _error("INPUT_MATERIAL_UNSAFE", "Local material reported an invalid file size", path=path)
        if before.st_size > MAX_INPUT_MATERIAL_BYTES:
            raise _error("INPUT_MATERIAL_TOO_LARGE", "Local material exceeds the individual raw limit", path=path)

        def chunks():
            consumed = 0
            while True:
                chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, max(1, MAX_INPUT_MATERIAL_BYTES - consumed + 1)))
                if not chunk:
                    return
                consumed += len(chunk)
                yield chunk

        raw, normalized = _normalize_snapshot_chunks(
            chunks(), aggregate_remaining=aggregate_remaining, path=path
        )
        after = os.fstat(descriptor)
        if _identity(after) != _identity(before):
            raise _error("INPUT_MATERIAL_UNSAFE", "Local material changed while being read", path=path)
        return CapturedMaterial(
            material=material.model_copy(deep=True),
            raw_bytes=raw,
            normalized_bytes=normalized,
            content_hash=digest(normalized),
            file_hash=None,
            identity=(before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns),
        )
    except OSError as exc:
        raise _error("INPUT_MATERIAL_UNSAFE", "Local material could not be read safely", path=path, value=str(exc)) from exc
    finally:
        os.close(descriptor)


def _capture(
    proposal_root: Path,
    expected_manifest: ProposalManifest | None,
    *,
    include_local: bool,
) -> ProposalSnapshot:
    root = Path(proposal_root)
    root_fd, root_identity = _open_root(root)
    try:
        try:
            manifest_bytes, manifest_identity = _read_exact(
                root_fd,
                root,
                PurePosixPath("manifest.json"),
                limit=MAX_IMMUTABLE_SNAPSHOT_BYTES,
            )
        except MemError as exc:
            if exc.detail.code != "INPUT_MATERIAL_TOO_LARGE":
                raise
            raise _error(
                "PROPOSAL_SNAPSHOT_TOO_LARGE",
                "Exact manifest bytes exceed the aggregate snapshot limit",
                path=root / "manifest.json",
                value={"limit": MAX_IMMUTABLE_SNAPSHOT_BYTES},
            ) from exc
        total = _budget(0, len(manifest_bytes))
        manifest = _parse_manifest(manifest_bytes, root)
        if manifest.review_id != root.name:
            raise _error("PROPOSAL_MANIFEST_MISMATCH", "Manifest review_id does not match its proposal root", path=root)
        if expected_manifest is not None and manifest.model_dump(mode="json") != expected_manifest.model_dump(mode="json"):
            raise _error("PROPOSAL_MANIFEST_MISMATCH", "Supplied manifest does not match exact manifest.json bytes", path=root / "manifest.json")

        before_tree = _enumerate_tree(root_fd, root)
        enumerated_manifest = next(
            (
                entry
                for entry in before_tree
                if entry.path == PurePosixPath("manifest.json")
                and entry.kind == "file"
            ),
            None,
        )
        if (
            enumerated_manifest is None
            or enumerated_manifest.identity != manifest_identity
        ):
            raise _error(
                "PROPOSAL_CHANGED_DURING_SNAPSHOT",
                "manifest.json changed between its read and proposal enumeration",
                path=root / "manifest.json",
            )
        _validate_closed_set(before_tree, manifest)
        business: list[CapturedBusinessFile] = []
        for listed in sorted(manifest.files, key=lambda item: item.path.as_posix().encode("utf-8")):
            data, identity = _read_exact(
                root_fd,
                root,
                listed.path,
                limit=MAX_INPUT_MATERIAL_BYTES,
                aggregate_remaining=MAX_IMMUTABLE_SNAPSHOT_BYTES - total,
            )
            total = _budget(total, len(data))
            business.append(CapturedBusinessFile(listed.path, listed.operation, data, digest(data), identity))

        materials: list[_MaterialValue] = []
        for descriptor in sorted(manifest.input_material, key=lambda item: item.material_id.encode("utf-8")):
            if isinstance(descriptor, LocalFileMaterial):
                if not include_local:
                    continue
                captured = _capture_local_material(
                    descriptor,
                    aggregate_remaining=MAX_IMMUTABLE_SNAPSHOT_BYTES - total,
                )
            else:
                raw, identity6 = _read_exact(root_fd, root, descriptor.path, limit=MAX_INPUT_MATERIAL_BYTES)
                raw_copy, normalized = _normalize_snapshot_chunks(
                    (raw[index : index + _READ_CHUNK_BYTES] for index in range(0, len(raw), _READ_CHUNK_BYTES)),
                    aggregate_remaining=MAX_IMMUTABLE_SNAPSHOT_BYTES - total,
                    path=root / Path(*descriptor.path.parts),
                )
                captured = CapturedMaterial(
                    material=descriptor.model_copy(deep=True),
                    raw_bytes=raw_copy,
                    normalized_bytes=normalized,
                    content_hash=digest(normalized),
                    file_hash=digest(raw_copy),
                    identity=(identity6[1], identity6[2], identity6[3], identity6[4]),
                )
            total = _budget(total, len(captured.normalized_bytes))
            descriptor_json = json.dumps(
                captured.material.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            materials.append(
                _MaterialValue(
                    descriptor_json,
                    captured.raw_bytes,
                    captured.normalized_bytes,
                    captured.content_hash,
                    captured.file_hash,
                    captured.identity,
                )
            )

        after_tree = _enumerate_tree(root_fd, root)
        try:
            named_root = os.stat(root, follow_symlinks=False)
        except OSError as exc:
            raise _error("PROPOSAL_CHANGED_DURING_SNAPSHOT", "Proposal root changed during capture", path=root, value=str(exc)) from exc
        if after_tree != before_tree or _identity(named_root) != root_identity:
            raise _error("PROPOSAL_CHANGED_DURING_SNAPSHOT", "Proposal tree changed during capture", path=root)
        return ProposalSnapshot(
            manifest_bytes=manifest_bytes,
            business_files=tuple(business),
            _input_material=tuple(materials),
            tree_entries=before_tree,
            total_bytes=total,
        )
    finally:
        os.close(root_fd)


def capture_snapshot(
    proposal_root: Path,
    manifest: ProposalManifest,
    *,
    include_local: bool,
) -> ProposalSnapshot:
    """Capture a closed proposal through one descriptor-pinned root."""

    return _capture(proposal_root, manifest, include_local=include_local)


def capture_snapshot_from_root(
    proposal_root: Path, *, include_local: bool
) -> ProposalSnapshot:
    """Capture and parse manifest.json without a preliminary path-based read."""

    return _capture(proposal_root, None, include_local=include_local)
