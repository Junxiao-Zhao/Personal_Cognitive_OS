"""Disposable retrieval generation construction and read-only search."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
from typing import Any, Iterable, Iterator, Mapping

from mem_core.errors import MemError
from mem_core.profile import Profile
from mem_core.registry import default_registry
from mem_core.repository import MemoryRepository

from .backlinks import build_reverse_references
from .derivation_manifest import (
    DerivationManifest,
    canonical_manifest_bytes,
    derivation_error,
    hash_bytes,
    manifest_for_files,
    strict_json_object,
)
from .projections import (
    _WrittenTreeIdentity,
    _require_named_identity,
    _require_path_identity,
    _stat_identity,
    _write_exclusive,
    _write_new_tree,
    exact_canonical_records,
)
from .paths import bundled_profile
from .validation import latest_effective

TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]")
SEARCH_MODES = frozenset({"current", "historical", "change", "pattern", "continuity"})
CANONICAL_FALLBACK_CANDIDATES = 200
BACKEND_CANDIDATES = 200
_SYSTEM_TEMP_PARENT = Path("/tmp")
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
)


@dataclass(frozen=True)
class _TreeSnapshot:
    files: dict[str, bytes]
    directories: frozenset[str]
    identities: dict[str, tuple[int, int, int]]


@dataclass
class _OpenedTree:
    identity: _WrittenTreeIdentity
    parent_fd: int
    root_fd: int

    @property
    def anchored_path(self) -> Path:
        return Path(f"/proc/self/fd/{self.root_fd}")

    def require_bound(self) -> None:
        if _stat_identity(os.fstat(self.parent_fd)) != self.identity.parent_identity:
            raise derivation_error(
                "Derivation target parent descriptor identity changed",
                path=self.identity.parent,
            )
        if _stat_identity(os.fstat(self.root_fd)) != self.identity.target_identity:
            raise derivation_error(
                "Derivation target descriptor identity changed",
                path=self.identity.target,
            )
        _require_path_identity(self.identity.parent, self.identity.parent_identity)
        _require_named_identity(
            self.parent_fd,
            self.identity.name,
            self.identity.target_identity,
            self.identity.target,
        )


@contextmanager
def _open_tree(
    target: Path, *, expected: _WrittenTreeIdentity | None = None
) -> Iterator[_OpenedTree]:
    target = Path(target).absolute()
    name = target.name
    parent = target.parent
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise derivation_error("Derivation target name is unsafe", path=target)
    try:
        parent_observed = os.lstat(parent)
        if not stat.S_ISDIR(parent_observed.st_mode):
            raise derivation_error(
                "Derivation target parent is not a real directory", path=parent
            )
        parent_identity = _stat_identity(parent_observed)
        flags = (
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        )
        parent_fd = os.open(parent, flags)
    except MemError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise derivation_error(
            "Derivation target parent cannot be opened safely",
            path=parent,
            value=str(exc),
        ) from exc
    root_fd: int | None = None
    try:
        if expected is not None:
            if (
                expected.target != target
                or expected.parent != parent
                or expected.name != name
            ):
                raise derivation_error(
                    "Derivation target identity token does not match its path",
                    path=target,
                )
            parent_identity = expected.parent_identity
        if _stat_identity(os.fstat(parent_fd)) != parent_identity:
            raise derivation_error(
                "Derivation target parent identity changed", path=parent
            )
        _require_path_identity(parent, parent_identity)
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(observed.st_mode):
            raise derivation_error(
                "Derivation target is not a real directory", path=target
            )
        target_identity = _stat_identity(observed)
        if expected is not None and target_identity != expected.target_identity:
            raise derivation_error(
                "Derivation target identity changed between construction phases",
                path=target,
            )
        identity = expected or _WrittenTreeIdentity(
            target=target,
            parent=parent,
            name=name,
            parent_identity=parent_identity,
            target_identity=target_identity,
        )
        root_fd = os.open(name, flags, dir_fd=parent_fd)
        opened = _OpenedTree(identity, parent_fd, root_fd)
        opened.require_bound()
        yield opened
        opened.require_bound()
    except MemError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise derivation_error(
            "Derivation target changed while it was open",
            path=target,
            value=str(exc),
        ) from exc
    finally:
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)


def tokenize(text: str) -> list[str]:
    base = [token.lower() for token in TOKEN_RE.findall(text)]
    cjk = [token for token in base if len(token) == 1 and "\u3400" <= token <= "\u9fff"]
    return base + [cjk[index] + cjk[index + 1] for index in range(len(cjk) - 1)]


def _snapshot_open_tree(tree: _OpenedTree) -> _TreeSnapshot:
    tree.require_bound()
    files: dict[str, bytes] = {}
    directories: set[str] = set()
    identities: dict[str, tuple[int, int, int]] = {}
    root_before = os.fstat(tree.root_fd)
    _scan_open_directory(
        tree.root_fd, PurePosixPath("."), files, directories, identities
    )
    root_after = os.fstat(tree.root_fd)
    if _file_version(root_before) != _file_version(root_after):
        raise derivation_error(
            "Derivation target changed while it was captured",
            path=tree.identity.target,
        )
    tree.require_bound()
    return _TreeSnapshot(files, frozenset(directories), identities)


def _scan_open_directory(
    directory_fd: int,
    prefix: PurePosixPath,
    files: dict[str, bytes],
    directories: set[str],
    identities: dict[str, tuple[int, int, int]],
) -> None:
    try:
        names = os.listdir(directory_fd)
    except OSError as exc:
        raise derivation_error(
            "Derivation directory cannot be enumerated", path=prefix.as_posix()
        ) from exc
    normalized: list[str] = []
    for name in names:
        try:
            name.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise derivation_error(
                "Derivation entry name is not valid UTF-8", path=prefix.as_posix()
            ) from exc
        if name in {".", ".."} or "/" in name or "\\" in name:
            raise derivation_error("Derivation entry name is unsafe", path=name)
        normalized.append(name)
    for name in sorted(normalized, key=str.encode):
        relative = (prefix / name).as_posix().removeprefix("./")
        try:
            observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise derivation_error(
                "Derivation entry changed during capture", path=relative
            ) from exc
        identity = _stat_identity(observed)
        identities[relative] = (*identity, stat.S_IFMT(observed.st_mode))
        if stat.S_ISDIR(observed.st_mode):
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
                    "Derivation directory changed during capture", path=relative
                ) from exc
            try:
                if _stat_identity(os.fstat(child_fd)) != identity:
                    raise derivation_error(
                        "Derivation directory identity changed", path=relative
                    )
                directories.add(relative)
                _scan_open_directory(
                    child_fd,
                    PurePosixPath(relative),
                    files,
                    directories,
                    identities,
                )
                _require_open_entry(directory_fd, name, identity, relative)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(observed.st_mode):
            files[relative] = _read_open_file(directory_fd, name, identity, relative)
        else:
            raise derivation_error(
                "Derivation tree contains a symlink or non-regular entry",
                path=relative,
            )
    try:
        final_names = os.listdir(directory_fd)
    except OSError as exc:
        raise derivation_error(
            "Derivation directory changed during capture", path=prefix.as_posix()
        ) from exc
    if set(final_names) != set(names):
        raise derivation_error(
            "Derivation directory contents changed during capture",
            path=prefix.as_posix(),
        )


def _read_open_file(
    parent_fd: int, name: str, identity: tuple[int, int], relative: str
) -> bytes:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise derivation_error(
            "Derivation file changed during capture", path=relative
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or _stat_identity(before) != identity:
            raise derivation_error("Derivation file identity changed", path=relative)
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
        _require_open_entry(parent_fd, name, identity, relative)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _require_open_entry(
    parent_fd: int, name: str, identity: tuple[int, int], relative: str
) -> None:
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise derivation_error(
            "Derivation entry changed during capture", path=relative
        ) from exc
    if _stat_identity(observed) != identity:
        raise derivation_error(
            "Derivation entry identity changed during capture", path=relative
        )


def _file_version(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _validate_generation_snapshot(
    snapshot: _TreeSnapshot,
    *,
    expected_commit: str,
    expected_profile_version: str,
) -> DerivationManifest:
    raw_manifest = snapshot.files.get("manifest.json")
    if raw_manifest is None:
        raise derivation_error("Retrieval generation has no manifest")
    parsed = strict_json_object(raw_manifest, path="manifest.json")
    try:
        manifest = DerivationManifest.model_validate(parsed, strict=True)
    except Exception as exc:
        raise derivation_error(
            "Retrieval generation manifest is invalid", value=str(exc)
        ) from exc
    if raw_manifest != canonical_manifest_bytes(manifest):
        raise derivation_error("Retrieval generation manifest is not canonical")
    if (
        manifest.kind != "retrieval_index"
        or manifest.source_commit != expected_commit
        or manifest.profile_version != expected_profile_version
    ):
        raise derivation_error(
            "Retrieval generation controls do not match canonical state",
            value={
                "kind": manifest.kind,
                "source_commit": manifest.source_commit,
                "profile_version": manifest.profile_version,
            },
        )
    payloads = {
        name: data for name, data in snapshot.files.items() if name != "manifest.json"
    }
    if set(payloads) != set(manifest.files):
        raise derivation_error("Retrieval generation payload set is not closed")
    if any(
        hash_bytes(payloads[name]) != digest for name, digest in manifest.files.items()
    ):
        raise derivation_error("Retrieval generation payload hash is invalid")
    expected_directories: set[str] = set()
    for relative in manifest.files:
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    if set(snapshot.directories) != expected_directories:
        raise derivation_error("Retrieval generation directory set is not closed")
    return manifest


def _vector(tokens: Iterable[str], dimensions: int = 256) -> list[float]:
    counts: Counter[int] = Counter()
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        counts[int.from_bytes(digest, "big") % dimensions] += 1
    norm = math.sqrt(sum(value * value for value in counts.values())) or 1.0
    return [float(counts.get(index, 0) / norm) for index in range(dimensions)]


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _document_text(record: Mapping[str, Any]) -> str:
    payload = record.get("payload", {})
    if not isinstance(payload, Mapping):
        return ""
    parts: list[str] = []
    for key in (
        "body_markdown",
        "name",
        "title",
        "locator",
        "provider_label",
        "change_summary",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    for key in (
        "used_excerpts",
        "aliases",
        "topics",
        "unanswered_questions",
        "active_tensions",
        "recent_decisions",
        "next_directions",
    ):
        values = payload.get(key)
        if isinstance(values, list):
            parts.extend(value for value in values if isinstance(value, str))
    return "\n".join(parts)


def _occurred_at(record: Mapping[str, Any]) -> str:
    payload = record.get("payload", {})
    if isinstance(payload, Mapping):
        occurred = payload.get("occurred_at")
        if isinstance(occurred, Mapping) and isinstance(occurred.get("start"), str):
            return occurred["start"]
        for key in ("retrieved_at", "created_at"):
            if isinstance(payload.get(key), str):
                return payload[key]
    recorded_at = record.get("recorded_at")
    return recorded_at if isinstance(recorded_at, str) else ""


def canonical_documents(
    repository: MemoryRepository,
    source_commit: str,
    *,
    canonical_head: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Build deterministic retrieval documents from exact Git-object bytes."""

    profile, records = exact_canonical_records(
        repository, source_commit, canonical_head=canonical_head
    )
    effective = {
        stream: {
            record_id: item["revision"]
            for record_id, item in latest_effective(items).items()
        }
        for stream, items in records.items()
    }
    documents: list[dict[str, Any]] = []
    for stream in sorted(records, key=str.encode):
        for record in records[stream]:
            payload = record.get("payload", {})
            if not isinstance(payload, Mapping):
                payload = {}
            revision = record.get("revision")
            record_id = record.get("id")
            documents.append(
                {
                    "stream": stream,
                    "id": record_id,
                    "revision": revision,
                    "text": _document_text(record),
                    "recorded_at": record.get("recorded_at"),
                    "occurred_at": _occurred_at(record),
                    "status": payload.get("status"),
                    "current": effective.get(stream, {}).get(record_id) == revision,
                    "previous_revision": payload.get("previous_revision"),
                    "change_summary": payload.get("change_summary"),
                    "evidence_refs": list(payload.get("evidence_refs", [])),
                    "counter_evidence_refs": list(
                        payload.get("counter_evidence_refs", [])
                    ),
                    "promotion_refs": list(payload.get("promotion_refs", [])),
                    "links": dict(payload.get("links", {})),
                }
            )
    return sorted(documents, key=_document_key), profile.version


def _document_key(document: Mapping[str, Any]) -> tuple[bytes, bytes, int]:
    return (
        str(document.get("stream", "")).encode("utf-8"),
        str(document.get("id", "")).encode("utf-8"),
        int(document.get("revision", 0)),
    )


def build_tantivy(path: Path, documents: list[dict[str, Any]]) -> None:
    import tantivy

    path.mkdir()
    builder = tantivy.SchemaBuilder()
    builder.add_text_field("key", stored=True, tokenizer_name="raw")
    builder.add_text_field("text", stored=True)
    builder.add_text_field("stream", stored=True, tokenizer_name="raw")
    schema = builder.build()
    index = tantivy.Index(schema, path=str(path))
    writer = index.writer()
    for document in documents:
        writer.add_document(
            tantivy.Document(
                key=_key(document),
                text=" ".join(tokenize(document["text"])),
                stream=document["stream"],
            )
        )
    writer.commit()
    del writer
    index.reload()


@contextmanager
def _local_backend_environment() -> Iterator[None]:
    previous = {name: os.environ.get(name) for name in ("NO_PROXY", "no_proxy")}
    try:
        for name, value in previous.items():
            entries = [
                item.strip() for item in (value or "").split(",") if item.strip()
            ]
            for item in ("127.0.0.1", "localhost"):
                if item not in entries:
                    entries.append(item)
            os.environ[name] = ",".join(entries)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def build_milvus(path: Path, documents: list[dict[str, Any]]) -> None:
    with _local_backend_environment():
        from pymilvus import MilvusClient

        client = None
        try:
            client = MilvusClient(str(path), dedicated=True)
            client.create_collection(
                "memory",
                dimension=256,
                primary_field_name="key",
                id_type="string",
                max_length=512,
                vector_field_name="vector",
                metric_type="COSINE",
            )
            if documents:
                client.insert(
                    "memory",
                    [
                        {
                            "key": _key(document),
                            "vector": _vector(tokenize(document["text"])),
                        }
                        for document in documents
                    ],
                )
                client.flush("memory")
        finally:
            _close_milvus(client, path)


def _close_milvus(client: Any, path: Path) -> None:
    """Close both the pymilvus channel and its path-keyed Lite server."""

    try:
        if client is not None:
            client.close()
    finally:
        from milvus_lite import server_manager_instance

        server_manager_instance.release_server(str(path))


def build_retrieval_generation(
    repository: MemoryRepository, target: Path, source_commit: str
) -> DerivationManifest:
    """Build one complete caller-owned generation without publishing it."""

    documents, profile_version = canonical_documents(repository, source_commit)
    target = Path(target).absolute()
    base_payloads = {
        "documents.json": _canonical_json(documents),
        "reverse_refs.json": _canonical_json(build_reverse_references(documents)),
    }
    written = _write_new_tree(
        target,
        base_payloads,
    )
    try:
        with _open_tree(target, expected=written) as tree:
            try:
                build_tantivy(tree.anchored_path / "tantivy", documents)
                build_milvus(tree.anchored_path / "milvus.db", documents)
            except Exception as exc:
                raise derivation_error(
                    "Retrieval backend construction failed",
                    path=target,
                    value=str(exc),
                ) from exc
            built = _snapshot_open_tree(tree)
            _require_builder_payloads(built.files, base_payloads, target)
            _materialize_empty_backend_directories(
                tree.root_fd, built.files, set(built.directories)
            )
            completed = _snapshot_open_tree(tree)
            _require_builder_payloads(completed.files, base_payloads, target)
            if "manifest.json" in completed.files:
                raise derivation_error(
                    "Retrieval target unexpectedly contains a manifest", path=target
                )
            manifest = manifest_for_files(
                completed.files,
                kind="retrieval_index",
                source_commit=source_commit,
                profile_version=profile_version,
            )
            _write_exclusive(
                tree.root_fd, "manifest.json", canonical_manifest_bytes(manifest)
            )
            final = _snapshot_open_tree(tree)
            observed = _validate_generation_snapshot(
                final,
                expected_commit=source_commit,
                expected_profile_version=profile_version,
            )
            if observed != manifest:
                raise derivation_error(
                    "Completed retrieval manifest changed during construction",
                    path=target,
                )
            _require_builder_payloads(final.files, base_payloads, target)
            return manifest
    except MemError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise derivation_error(
            "Retrieval generation could not be built safely",
            path=target,
            value=str(exc),
        ) from exc


def _require_builder_payloads(
    payloads: Mapping[str, bytes], base_payloads: Mapping[str, bytes], target: Path
) -> None:
    if any(payloads.get(name) != content for name, content in base_payloads.items()):
        raise derivation_error(
            "Retrieval generation lost or changed a required canonical payload",
            path=target,
        )
    has_tantivy = any(name.startswith("tantivy/") for name in payloads)
    has_milvus = any(
        name == "milvus.db" or name.startswith("milvus.db/") for name in payloads
    )
    if not has_tantivy or not has_milvus:
        raise derivation_error(
            "Retrieval generation is missing a required backend payload",
            path=target,
            value={"tantivy": has_tantivy, "milvus": has_milvus},
        )


def _materialize_empty_backend_directories(
    root_fd: int, payloads: Mapping[str, bytes], directories: set[str]
) -> None:
    """Make backend-created empty directories expressible by the file manifest."""

    implied: set[str] = set()
    for relative in payloads:
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != ".":
            implied.add(parent.as_posix())
            parent = parent.parent
    for relative in sorted(directories - implied, key=str.encode):
        descriptor = os.dup(root_fd)
        try:
            for component in PurePosixPath(relative).parts:
                child = os.open(
                    component,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
            marker = os.open(
                ".pco-empty",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=descriptor,
            )
            try:
                os.write(marker, b"pco retrieval backend directory\n")
                os.fsync(marker)
            finally:
                os.close(marker)
        finally:
            os.close(descriptor)


def _key(document: Mapping[str, Any]) -> str:
    return f"{document['stream']}:{document['id']}@{document['revision']}"


def _snapshot_directory_fd(root_fd: int) -> _TreeSnapshot:
    files: dict[str, bytes] = {}
    directories: set[str] = set()
    identities: dict[str, tuple[int, int, int]] = {}
    root_before = os.fstat(root_fd)
    _scan_open_directory(root_fd, PurePosixPath("."), files, directories, identities)
    root_after = os.fstat(root_fd)
    if _file_version(root_before) != _file_version(root_after):
        raise derivation_error("Private Tantivy snapshot changed during capture")
    return _TreeSnapshot(files, frozenset(directories), identities)


def _named_directory_matches(
    parent_fd: int, name: str, identity: tuple[int, int]
) -> bool:
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(observed.st_mode) and _stat_identity(observed) == identity


def _erase_open_directory(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        identity = _stat_identity(observed)
        if stat.S_ISDIR(observed.st_mode):
            child_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
            try:
                if _stat_identity(os.fstat(child_fd)) != identity:
                    raise OSError("private query directory identity changed")
                _erase_open_directory(child_fd)
            finally:
                os.close(child_fd)
            if _named_directory_matches(directory_fd, name, identity):
                os.rmdir(name, dir_fd=directory_fd)
        elif stat.S_ISREG(observed.st_mode):
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISREG(current.st_mode) and _stat_identity(current) == identity:
                os.unlink(name, dir_fd=directory_fd)
        else:
            raise OSError("private query tree contains a non-owned entry")


def _workspace_root(canonical_root: Path) -> Path:
    canonical = Path(canonical_root).resolve(strict=True)
    if canonical.parent.name == ".pco":
        return canonical.parent.parent
    return canonical.parent


@contextmanager
def _private_tantivy_snapshot(
    snapshot: _TreeSnapshot, *, forbidden_roots: Iterable[Path]
) -> Iterator[Path]:
    payloads = {
        relative: content
        for relative, content in snapshot.files.items()
        if relative.startswith("tantivy/")
    }
    if not payloads:
        raise derivation_error("Retrieval snapshot has no Tantivy payload")
    system_fd: int | None = None
    parent_fd: int | None = None
    root_fd: int | None = None
    parent_name: str | None = None
    parent_identity: tuple[int, int] | None = None
    root_identity: tuple[int, int] | None = None
    try:
        system_parent = Path(_SYSTEM_TEMP_PARENT).absolute()
        observed = os.lstat(system_parent)
        if not stat.S_ISDIR(observed.st_mode):
            raise derivation_error(
                "System temporary parent is not a real directory",
                path=system_parent,
            )
        system_identity = _stat_identity(observed)
        resolved_parent = system_parent.resolve(strict=True)
        resolved_forbidden = [
            Path(path).resolve(strict=True) for path in forbidden_roots
        ]
        if any(resolved_parent.is_relative_to(path) for path in resolved_forbidden):
            raise derivation_error(
                "System temporary parent is inside protected workspace state",
                path=system_parent,
            )
        system_fd = os.open(system_parent, _DIRECTORY_OPEN_FLAGS)
        if _stat_identity(os.fstat(system_fd)) != system_identity:
            raise derivation_error(
                "System temporary parent identity changed", path=system_parent
            )
        _require_path_identity(system_parent, system_identity)

        for _ in range(128):
            candidate = f"pco-query-{secrets.token_hex(16)}"
            try:
                os.mkdir(candidate, mode=0o700, dir_fd=system_fd)
            except FileExistsError:
                continue
            parent_name = candidate
            break
        if parent_name is None:
            raise derivation_error(
                "Private query parent name could not be allocated",
                path=system_parent,
            )
        parent_observed = os.stat(parent_name, dir_fd=system_fd, follow_symlinks=False)
        parent_identity = _stat_identity(parent_observed)
        parent_fd = os.open(parent_name, _DIRECTORY_OPEN_FLAGS, dir_fd=system_fd)
        if _stat_identity(os.fstat(parent_fd)) != parent_identity:
            raise derivation_error("Private query parent identity changed")

        os.mkdir("generation", mode=0o700, dir_fd=parent_fd)
        root_observed = os.stat("generation", dir_fd=parent_fd, follow_symlinks=False)
        root_identity = _stat_identity(root_observed)
        root_fd = os.open("generation", _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
        if _stat_identity(os.fstat(root_fd)) != root_identity:
            raise derivation_error("Private query root identity changed")
        for relative, content in sorted(
            payloads.items(), key=lambda item: item[0].encode("utf-8")
        ):
            _write_exclusive(root_fd, relative, content)
        before = _snapshot_directory_fd(root_fd)
        if before.files != payloads:
            raise derivation_error(
                "Private Tantivy snapshot does not match verified payload"
            )
        yield Path(f"/proc/self/fd/{root_fd}")
        after = _snapshot_directory_fd(root_fd)
        if after != before:
            raise derivation_error("Private Tantivy snapshot changed during query")
    except MemError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise derivation_error(
            "Private Tantivy snapshot could not be managed safely", value=str(exc)
        ) from exc
    finally:
        cleanup_error: Exception | None = None
        if root_fd is not None:
            try:
                _erase_open_directory(root_fd)
            except (OSError, TypeError, ValueError) as exc:
                cleanup_error = exc
            finally:
                os.close(root_fd)
        if (
            parent_fd is not None
            and root_identity is not None
            and _named_directory_matches(parent_fd, "generation", root_identity)
        ):
            try:
                os.rmdir("generation", dir_fd=parent_fd)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if parent_fd is not None:
            os.close(parent_fd)
        if (
            system_fd is not None
            and parent_name is not None
            and parent_identity is not None
            and _named_directory_matches(system_fd, parent_name, parent_identity)
        ):
            try:
                os.rmdir(parent_name, dir_fd=system_fd)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if system_fd is not None:
            os.close(system_fd)
        if cleanup_error is not None:
            raise derivation_error(
                "Private Tantivy snapshot could not be cleaned safely",
                value=str(cleanup_error),
            ) from cleanup_error


def query_backends(
    generation: Path,
    query: str,
    eligible_keys: set[str],
    candidate_limit: int,
) -> tuple[dict[str, float], dict[str, float]]:
    """Query only backends whose open path is demonstrably read-only.

    The supported Milvus Lite release recreates operational staging state when
    a local database is opened, invalidating the generation it is meant to
    query.  Its deterministic artifact remains part of the rebuildable output,
    while read paths use Tantivy and the in-memory deterministic reranker.
    """

    dense: dict[str, float] = {}
    lexical: dict[str, float] = {}
    if query.strip():
        import tantivy

        index = tantivy.Index.open(str(generation / "tantivy"))
        parsed = index.parse_query(" OR ".join(tokenize(query)), ["text"])
        searcher = index.searcher()
        for score, address in searcher.search(parsed, limit=candidate_limit).hits:
            key = str(searcher.doc(address).get_first("key"))
            if key in eligible_keys:
                lexical[key] = float(score)
    return dense, lexical


def search(
    *,
    repo_root: Path,
    query: str = "",
    mode: str = "current",
    limit: int = 10,
    indexes_root: Path | None = None,
    start: str | None = None,
    end: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Search a verified generation or a bounded exact-commit canonical snapshot."""

    start_at, end_at = _validate_search(query, mode, limit, start, end)
    repository = MemoryRepository(
        Path(repo_root), Profile.load(bundled_profile(), default_registry())
    )
    head = repository.head()
    generation = (
        Path(indexes_root)
        if indexes_root is not None
        else Path(repo_root).parent / "indexes"
    )
    canonical, profile_version = canonical_documents(
        repository, head, canonical_head=head
    )
    canonical_payloads = {
        "documents.json": _canonical_json(canonical),
        "reverse_refs.json": _canonical_json(build_reverse_references(canonical)),
    }
    status = "current"
    diagnostics: dict[str, Any] | None = None
    change_windows: dict[str, Any] | None = None
    try:
        with _open_tree(generation) as tree:
            initial = _snapshot_open_tree(tree)
            _validate_generation_snapshot(
                initial,
                expected_commit=head,
                expected_profile_version=profile_version,
            )
            _require_builder_payloads(initial.files, canonical_payloads, generation)
            documents = _read_json_list_bytes(initial.files["documents.json"])
            reverse_refs = _read_reverse_refs_bytes(initial.files["reverse_refs.json"])
            selected = _select_mode(documents, mode, start_at, end_at)
            with _private_tantivy_snapshot(
                initial,
                forbidden_roots=(
                    _workspace_root(Path(repo_root)),
                    Path(repo_root),
                    generation,
                ),
            ) as query_root:
                backend_scores = query_backends(
                    query_root,
                    query,
                    {_key(document) for document in selected},
                    min(BACKEND_CANDIDATES, max(limit, 1) * 4),
                )
            items = _rank(selected, reverse_refs, query, mode, limit, backend_scores)
            if mode == "change":
                change_windows = _change_window_summary(selected)
            final = _snapshot_open_tree(tree)
            if final != initial:
                raise derivation_error(
                    "Retrieval generation changed while search consumed it",
                    path=generation,
                )
    except Exception as exc:
        status = "fallback"
        diagnostics = {
            "code": "DERIVATION_FAILED",
            "message": "retrieval generation unavailable; canonical fallback used",
            "value": str(exc),
        }
        documents = canonical
        reverse_refs = build_reverse_references(documents)
        selected = _select_mode(documents, mode, start_at, end_at)
        selected = _bounded_recent(selected, CANONICAL_FALLBACK_CANDIDATES)
        items = _rank(selected, reverse_refs, query, mode, limit, ({}, {}))
        if mode == "change":
            change_windows = _change_window_summary(selected)
    result: dict[str, Any] = {
        "ok": True,
        "query": query,
        "mode": mode,
        "source_commit": head,
        "derivation_status": status,
        "candidate_count": len(selected),
        "items": items,
    }
    if diagnostics is not None:
        result["fallback_diagnostic"] = diagnostics
    if change_windows is not None:
        result["change_windows"] = change_windows
    return result


def _validate_search(
    query: Any, mode: Any, limit: Any, start: Any, end: Any
) -> tuple[datetime | None, datetime | None]:
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    if mode not in SEARCH_MODES:
        raise ValueError(f"unknown retrieval mode: {mode}")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 1
        or limit > 1000
    ):
        raise ValueError("limit must be an integer from 1 through 1000")
    start_at = _parse_time_bound(start, "start")
    end_at = _parse_time_bound(end, "end")
    if start_at is not None and end_at is not None and start_at > end_at:
        raise ValueError("start must not be later than end")
    return start_at, end_at


def _parse_time_bound(value: Any, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 string") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _record_time(document: Mapping[str, Any]) -> datetime | None:
    value = document.get("occurred_at") or document.get("recorded_at")
    try:
        return _parse_time_bound(value, "record time") if value else None
    except ValueError:
        return None


def _select_mode(
    documents: list[dict[str, Any]],
    mode: str,
    start: datetime | None,
    end: datetime | None,
) -> list[dict[str, Any]]:
    if mode in {"historical", "change"}:
        selected = list(documents)
    else:
        selected = [document for document in documents if document.get("current")]
    if mode == "continuity":
        handoffs = [
            document
            for document in selected
            if document["stream"] == "session_handoffs"
        ]
        selected = [max(handoffs, key=_recent_key)] if handoffs else []
    elif mode == "pattern":
        selected = [
            document
            for document in selected
            if document["stream"]
            in {
                "events",
                "psychologies",
                "philosophies",
                "archetypes",
                "hypotheses",
            }
        ]
    if mode == "current":
        selected = [
            document for document in selected if document.get("status") != "tombstone"
        ]
    result = []
    for document in selected:
        when = _record_time(document)
        if start is not None and (when is None or when < start):
            continue
        if end is not None and (when is None or when > end):
            continue
        result.append(dict(document))
    return sorted(result, key=_document_key)


def _recent_key(document: Mapping[str, Any]) -> tuple[str, int, bytes]:
    return (
        str(document.get("recorded_at") or ""),
        int(document.get("revision") or 0),
        str(document.get("id") or "").encode("utf-8"),
    )


def _bounded_recent(
    documents: list[dict[str, Any]], maximum: int
) -> list[dict[str, Any]]:
    return sorted(
        documents,
        key=lambda item: (_recent_key(item), _document_key(item)),
        reverse=True,
    )[:maximum]


def _rank(
    documents: list[dict[str, Any]],
    reverse_refs: dict[str, list[dict[str, Any]]],
    query: str,
    mode: str,
    limit: int,
    backend_scores: tuple[dict[str, float], dict[str, float]],
) -> list[dict[str, Any]]:
    dense, backend_lexical = backend_scores
    terms = tokenize(query)
    seeds: set[str] = set()
    scored: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for document in documents:
        tokens = tokenize(document.get("text", ""))
        lexical_score = sum(tokens.count(term) for term in terms)
        key = _key(document)
        score = (
            float(lexical_score) + dense.get(key, 0.0) + backend_lexical.get(key, 0.0)
        )
        if query and lexical_score:
            seeds.add(str(document["id"]))
        item = dict(document)
        item["score"] = round(score, 8)
        scored.append(
            (
                (
                    score,
                    float(lexical_score),
                    str(document.get("recorded_at") or ""),
                    int(document.get("revision") or 0),
                ),
                item,
            )
        )
    if mode == "pattern" and seeds:
        linked = {
            edge["source_id"] for seed in seeds for edge in reverse_refs.get(seed, [])
        }
        for index, (sort_key, item) in enumerate(scored):
            if item["id"] in linked:
                item["score"] = round(item["score"] + 0.25, 8)
                scored[index] = ((sort_key[0] + 0.25, *sort_key[1:]), item)
    scored.sort(key=lambda pair: _document_key(pair[1]))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _sort_key, item in scored[:limit]]


def _read_json_list_bytes(data: bytes) -> list[dict[str, Any]]:
    value = _read_json_bytes(data)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("documents.json must contain an array of objects")
    return [dict(item) for item in value]


def _read_reverse_refs_bytes(data: bytes) -> dict[str, list[dict[str, Any]]]:
    value = _read_json_bytes(data)
    if not isinstance(value, dict):
        raise ValueError("reverse_refs.json must contain an object")
    result: dict[str, list[dict[str, Any]]] = {}
    for target, edges in value.items():
        if (
            not isinstance(target, str)
            or not isinstance(edges, list)
            or not all(isinstance(edge, dict) for edge in edges)
        ):
            raise ValueError("reverse_refs.json is malformed")
        result[target] = [dict(edge) for edge in edges]
    return result


def _read_json_bytes(data: bytes) -> Any:
    class StrictObject(dict):
        pass

    def pairs(values):
        result = StrictObject()
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def nonfinite(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("retrieval payload must be UTF-8") from exc
    return json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)


def _change_window_summary(documents: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        (
            (when, item)
            for item in documents
            if (when := _record_time(item)) is not None
        ),
        key=lambda pair: (pair[0], _document_key(pair[1])),
    )
    if not ordered:
        return {"split_at": None, "earlier": {}, "later": {}}
    split = ordered[len(ordered) // 2][0]
    earlier: Counter[str] = Counter()
    later: Counter[str] = Counter()
    for when, document in ordered:
        (earlier if when <= split else later)[document["stream"]] += 1
    return {
        "split_at": split.isoformat(),
        "earlier": dict(sorted(earlier.items())),
        "later": dict(sorted(later.items())),
    }


# Transitional entry point retained until Task 5 removes the package inventory.
def build_index(
    *,
    repo_root: Path,
    indexes_root: str | Path,
    source_commit: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    repository = MemoryRepository(
        Path(repo_root), Profile.load(bundled_profile(), default_registry())
    )
    commit = source_commit or repository.head()
    manifest = build_retrieval_generation(repository, Path(indexes_root), commit)
    return {"ok": True, **manifest.model_dump(mode="json")}
