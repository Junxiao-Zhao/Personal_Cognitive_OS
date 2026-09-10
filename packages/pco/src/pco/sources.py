from __future__ import annotations

import difflib
import hashlib
import json
import mimetypes
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, TypedDict
from urllib.parse import unquote, urlparse

from mem_core.errors import MemError, ensure
from mem_core.models import Operation, utc_now
from mem_core.transaction import TransactionManager

from .proposals.materials import (
    MAX_INPUT_MATERIAL_BYTES,
    _local_file_path,
    _material_error,
    digest as material_digest,
    normalize_utf8,
    read_regular_file,
)
from .validation import latest_effective
from .workspace import Workspace

_CONTENT_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_URI_SCHEME = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*):")


def _source_diff_error(
    code: str,
    message: str,
    *,
    path: str | None = None,
    record_id: str | None = None,
    value: Any = None,
) -> MemError:
    recovery = (
        ["Run pco source diff without an ID to list current sources"]
        if code == "SOURCE_NOT_FOUND"
        else ["Restore canonical memory from a trusted copy and retry source diff"]
    )
    return MemError(
        code,
        "source_diff",
        message,
        path=path,
        record_id=record_id,
        value=value,
        recovery=recovery,
        canonical_changed=False,
        retry_safe=False,
    )


def _canonical_object(
    workspace: Workspace,
    commit: str,
    relative: str,
    *,
    snapshot: bool,
) -> bytes:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(workspace.repository.root),
                "show",
                f"{commit}:{relative}",
            ],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        code = "SOURCE_SNAPSHOT_INVALID" if snapshot else "WORKSPACE_INTEGRITY_ERROR"
        raise _source_diff_error(
            code,
            "Canonical Git object cannot be read",
            path=relative,
            value=str(exc),
        ) from exc
    if result.returncode:
        code = "SOURCE_SNAPSHOT_INVALID" if snapshot else "WORKSPACE_INTEGRITY_ERROR"
        raise _source_diff_error(
            code,
            (
                "Canonical source snapshot is missing"
                if snapshot
                else "Canonical source stream is missing"
            ),
            path=relative,
            value=result.stderr.decode("utf-8", errors="replace").strip(),
        )
    return result.stdout


def _canonical_source_records(
    workspace: Workspace, commit: str
) -> list[dict[str, Any]]:
    relative = workspace.profile.config.streams["sources"].path
    raw = _canonical_object(workspace, commit, relative, snapshot=False)
    try:
        text = raw.decode("utf-8", errors="strict")
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _source_diff_error(
            "WORKSPACE_INTEGRITY_ERROR",
            "Canonical source stream is not valid UTF-8 JSONL",
            path=relative,
            value=str(exc),
        ) from exc
    if not all(isinstance(record, dict) for record in records):
        raise _source_diff_error(
            "WORKSPACE_INTEGRITY_ERROR",
            "Canonical source stream contains a non-object record",
            path=relative,
        )
    return records


def _selected_current_sources(
    workspace: Workspace, commit: str, source_id: str | None
) -> list[dict[str, Any]]:
    try:
        current = latest_effective(_canonical_source_records(workspace, commit))
    except (KeyError, TypeError, ValueError) as exc:
        raise _source_diff_error(
            "WORKSPACE_INTEGRITY_ERROR",
            "Canonical source revisions cannot be indexed",
            value=str(exc),
        ) from exc
    active = {
        record_id: dict(record)
        for record_id, record in current.items()
        if isinstance(record.get("payload"), Mapping)
        and record["payload"].get("status") == "active"
    }
    if source_id is not None:
        selected = active.get(source_id)
        if selected is None:
            raise _source_diff_error(
                "SOURCE_NOT_FOUND",
                "No current effective source has the requested ID",
                record_id=source_id,
                value=source_id,
            )
        return [selected]
    return [
        active[key] for key in sorted(active, key=lambda item: item.encode("utf-8"))
    ]


def _verified_snapshot(
    workspace: Workspace, commit: str, revision: Mapping[str, Any]
) -> bytes:
    source_id = revision.get("id")
    payload = revision.get("payload")
    if not isinstance(source_id, str) or not isinstance(payload, Mapping):
        raise _source_diff_error(
            "SOURCE_SNAPSHOT_INVALID",
            "Current source revision has no valid snapshot metadata",
            record_id=source_id if isinstance(source_id, str) else None,
        )
    content_hash_value = payload.get("content_hash")
    snapshot_ref = payload.get("snapshot_ref")
    expected_ref = (
        "source_blobs/" + content_hash_value.removeprefix("sha256:")
        if isinstance(content_hash_value, str)
        and _CONTENT_HASH.fullmatch(content_hash_value)
        else None
    )
    if not isinstance(snapshot_ref, str) or snapshot_ref != expected_ref:
        raise _source_diff_error(
            "SOURCE_SNAPSHOT_INVALID",
            "Source snapshot path does not agree with its content hash",
            path=snapshot_ref if isinstance(snapshot_ref, str) else None,
            record_id=source_id,
            value={"content_hash": content_hash_value, "snapshot_ref": snapshot_ref},
        )
    blob = _canonical_object(workspace, commit, snapshot_ref, snapshot=True)
    observed = material_digest(blob)
    if observed != content_hash_value:
        raise _source_diff_error(
            "SOURCE_SNAPSHOT_INVALID",
            "Canonical source snapshot bytes do not match their content hash",
            path=snapshot_ref,
            record_id=source_id,
            value={"expected": content_hash_value, "observed": observed},
        )
    try:
        blob.decode("utf-8", errors="strict")
        normalized = normalize_utf8([blob], limit=MAX_INPUT_MATERIAL_BYTES)
    except (UnicodeDecodeError, MemError) as exc:
        raise _source_diff_error(
            "SOURCE_SNAPSHOT_INVALID",
            "Canonical source snapshot is not normalized UTF-8 text",
            path=snapshot_ref,
            record_id=source_id,
        ) from exc
    if normalized != blob:
        raise _source_diff_error(
            "SOURCE_SNAPSHOT_INVALID",
            "Canonical source snapshot is not normalized UTF-8 text",
            path=snapshot_ref,
            record_id=source_id,
        )
    return blob


def _diff_one_source(
    workspace: Workspace, commit: str, revision: Mapping[str, Any]
) -> dict[str, Any]:
    source_id = revision["id"]
    payload = revision["payload"]
    locator = payload.get("locator")
    scheme = _URI_SCHEME.match(locator) if isinstance(locator, str) else None
    if scheme is None or scheme.group("scheme").lower() != "file":
        return {"source_id": source_id, "refresh_required": True}

    previous = _verified_snapshot(workspace, commit, revision)
    try:
        local_path = _local_file_path(locator)
    except ValueError as exc:
        raise _material_error(
            "INPUT_MATERIAL_UNSAFE",
            "Local input material URI has an invalid authority",
            value=locator,
        ) from exc
    current = read_regular_file(local_path, limit=MAX_INPUT_MATERIAL_BYTES)
    diff_bytes = b"".join(
        difflib.diff_bytes(
            difflib.unified_diff,
            previous.splitlines(keepends=True),
            current.normalized_bytes.splitlines(keepends=True),
            fromfile=f"{source_id}@previous".encode("utf-8"),
            tofile=f"{source_id}@current".encode("utf-8"),
        )
    )
    return {
        "source_id": source_id,
        "current_content_hash": current.content_hash,
        "diff": diff_bytes.decode("utf-8", errors="strict"),
    }


def source_diff(workspace: Workspace, source_id: str | None = None) -> dict[str, Any]:
    """Compare current local material with its verified immutable snapshot."""

    try:
        commit = workspace.repository.head()
    except OSError as exc:
        raise _source_diff_error(
            "WORKSPACE_INTEGRITY_ERROR",
            "Canonical Git HEAD cannot be read",
            value=str(exc),
        ) from exc
    revisions = _selected_current_sources(workspace, commit, source_id)
    return {
        "ok": True,
        "sources": [
            _diff_one_source(workspace, commit, revision) for revision in revisions
        ],
    }


class SourceRead(TypedDict):
    """The private boundary between a source reader and the wrapper.

    ``read_metadata`` is deliberately not part of a source record. Readers
    may need to return useful diagnostics (or credentials-related details)
    to the wrapper, but that data must not become canonical Git content.
    """

    locator: str
    reader: str
    normalized_content: str
    media_type: str
    read_metadata: dict[str, Any]


SourceReader = Callable[[str], Mapping[str, Any]]


def normalize_text(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def content_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _validate_locator(locator: str) -> str:
    ensure(
        isinstance(locator, str) and locator.strip(),
        "SOURCE_LOCATOR_INVALID",
        "source",
        "Source locator must be a non-empty string",
        canonical_changed=False,
        retry_safe=False,
    )
    parsed = urlparse(locator)
    ensure(
        parsed.scheme and parsed.scheme not in {"data", "javascript"},
        "SOURCE_LOCATOR_INVALID",
        "source",
        "Source locator must use a supported URI scheme",
        value=locator,
        canonical_changed=False,
        retry_safe=False,
    )
    if parsed.scheme == "file":
        ensure(
            parsed.path,
            "SOURCE_LOCATOR_INVALID",
            "source",
            "File locator must include a path",
            value=locator,
            canonical_changed=False,
            retry_safe=False,
        )
    elif parsed.scheme in {"http", "https"}:
        ensure(
            parsed.netloc,
            "SOURCE_LOCATOR_INVALID",
            "source",
            "HTTP(S) locator must include a host",
            value=locator,
            canonical_changed=False,
            retry_safe=False,
        )
    return locator


def read_local_file(locator: str) -> SourceRead:
    """Built-in read-only reader for ``file:`` locators."""

    _validate_locator(locator)
    parsed = urlparse(locator)
    ensure(
        parsed.scheme == "file",
        "SOURCE_READER_LOCATOR_MISMATCH",
        "source",
        "The local reader only accepts file locators",
        value=locator,
        canonical_changed=False,
        retry_safe=False,
    )
    path = Path(unquote(parsed.path))
    ensure(
        path.is_file(),
        "SOURCE_NOT_READABLE",
        "source",
        f"Registered source is unavailable: {path}",
        value=str(path),
        canonical_changed=False,
        retry_safe=False,
    )
    raw_content = path.read_text(encoding="utf-8")
    media_type = mimetypes.guess_type(path.name)[0] or "text/plain"
    return {
        "locator": locator,
        "reader": "local-readonly",
        "normalized_content": normalize_text(raw_content),
        "media_type": media_type,
        "read_metadata": {"path": str(path)},
    }


class ReaderRegistry:
    """Allowlisted source readers owned by the PCO wrapper.

    A registry is intentionally injectable: integrations can register an
    AFFiNE/Feishu/fixture reader without making SourceManager know about the
    remote platform or making a network call itself.
    """

    def __init__(self, readers: Mapping[str, SourceReader] | None = None) -> None:
        self._readers: dict[str, SourceReader] = {"local-readonly": read_local_file}
        for name, reader in (readers or {}).items():
            self.register(name, reader)

    @classmethod
    def from_profile(cls, profile: Any) -> "ReaderRegistry":
        """Load optional reader names from a Profile's explicit allowlist.

        Profiles may declare ``source_readers`` as ``name: registry-entry``.
        The target is resolved through mem-core's ProfileRegistry, so a YAML
        profile cannot import an arbitrary reader. The built-in local reader
        is always available even when the profile has no such declaration.
        """

        result = cls()
        for name, target in profile.raw.get("source_readers", {}).items():
            reader = (
                target if callable(target) else profile.registry.resolve(str(target))
            )
            result.register(str(name), reader)
        return result

    def register(self, name: str, reader: SourceReader) -> None:
        ensure(
            isinstance(name, str) and name.strip(),
            "SOURCE_READER_INVALID",
            "source",
            "Reader name must be non-empty",
            canonical_changed=False,
            retry_safe=False,
        )
        ensure(
            callable(reader),
            "SOURCE_READER_INVALID",
            "source",
            f"Reader is not callable: {name}",
            value=name,
            canonical_changed=False,
            retry_safe=False,
        )
        ensure(
            name not in self._readers,
            "SOURCE_READER_DUPLICATE",
            "source",
            f"Reader is already registered: {name}",
            value=name,
            canonical_changed=False,
            retry_safe=False,
        )
        self._readers[name] = reader

    def resolve(self, name: str) -> SourceReader:
        try:
            return self._readers[name]
        except KeyError as exc:
            raise MemError(
                "SOURCE_READER_REQUIRED",
                "source",
                f"No registered reader named {name}",
                value=name,
                canonical_changed=False,
                retry_safe=True,
                recovery=["Register the source reader before materializing the source"],
            ) from exc

    def read(self, *, reader: str, locator: str) -> SourceRead:
        result = self.resolve(reader)(locator)
        ensure(
            isinstance(result, Mapping),
            "SOURCE_READER_CONTRACT_INVALID",
            "source",
            "Source reader must return a mapping",
            value=reader,
            canonical_changed=False,
            retry_safe=False,
        )
        required = {
            "locator",
            "reader",
            "normalized_content",
            "media_type",
            "read_metadata",
        }
        ensure(
            set(result) == required,
            "SOURCE_READER_CONTRACT_INVALID",
            "source",
            "Source reader returned fields outside the materialization contract",
            value={"reader": reader, "fields": sorted(result)},
            canonical_changed=False,
            retry_safe=False,
        )
        ensure(
            isinstance(result["locator"], str),
            "SOURCE_READER_CONTRACT_INVALID",
            "source",
            "Reader locator must be a string",
            value=reader,
            canonical_changed=False,
            retry_safe=False,
        )
        ensure(
            isinstance(result["reader"], str) and result["reader"] == reader,
            "SOURCE_READER_CONTRACT_INVALID",
            "source",
            "Reader must identify itself with its registry name",
            value=reader,
            canonical_changed=False,
            retry_safe=False,
        )
        ensure(
            isinstance(result["normalized_content"], str),
            "SOURCE_READER_CONTRACT_INVALID",
            "source",
            "Reader content must be text",
            value=reader,
            canonical_changed=False,
            retry_safe=False,
        )
        ensure(
            isinstance(result["media_type"], str) and result["media_type"].strip(),
            "SOURCE_READER_CONTRACT_INVALID",
            "source",
            "Reader media_type must be non-empty",
            value=reader,
            canonical_changed=False,
            retry_safe=False,
        )
        ensure(
            isinstance(result["read_metadata"], Mapping),
            "SOURCE_READER_CONTRACT_INVALID",
            "source",
            "Reader read_metadata must be a mapping",
            value=reader,
            canonical_changed=False,
            retry_safe=False,
        )
        return {
            "locator": result["locator"],
            "reader": result["reader"],
            "normalized_content": result["normalized_content"],
            "media_type": result["media_type"],
            "read_metadata": dict(result["read_metadata"]),
        }


class SourceManager:
    def __init__(
        self, workspace: Workspace, reader_registry: ReaderRegistry | None = None
    ) -> None:
        self.workspace = workspace
        self.reader_registry = reader_registry or ReaderRegistry.from_profile(
            workspace.profile
        )

    def register_reader(self, name: str, reader: SourceReader) -> None:
        """Register a wrapper-owned reader without changing the Profile."""

        self.reader_registry.register(name, reader)

    def _register_locator(
        self,
        locator: str,
        *,
        reader_skill: str,
        provider: str,
        display_name: str,
        snapshot_path: str | None = None,
    ) -> dict[str, Any]:
        _validate_locator(locator)
        for record in self.workspace.repository.current_records("sources").values():
            if (
                record["payload"].get("locator") == locator
                and record["payload"].get("status") == "active"
            ):
                existing = record["payload"]
                same_registration = (
                    existing.get("reader_skill") == reader_skill
                    and existing.get("provider") == provider
                    and existing.get("display_name") == display_name
                )
                ensure(
                    same_registration,
                    "SOURCE_LOCATOR_CONFLICT",
                    "source",
                    "An active source with this locator is registered with different reader, provider, or name",
                    value={
                        "locator": locator,
                        "existing": {
                            "reader": existing.get("reader_skill"),
                            "provider": existing.get("provider"),
                            "name": existing.get("display_name"),
                        },
                        "requested": {
                            "reader": reader_skill,
                            "provider": provider,
                            "name": display_name,
                        },
                    },
                    recovery=[
                        "Reuse the existing source registration",
                        "Choose a different locator",
                    ],
                    canonical_changed=False,
                    retry_safe=False,
                )
                return {
                    "ok": True,
                    "idempotent": True,
                    "source_id": record["id"],
                    "commit": None,
                    "record": record,
                }
        # Resolve only for a new registration. An existing active locator is
        # intentionally idempotent even while its external reader is
        # temporarily unavailable.
        self.reader_registry.resolve(reader_skill)
        source_id = f"src_{uuid.uuid4().hex}"
        suffix = Path(urlparse(locator).path).suffix or ".md"
        record = {
            "id": source_id,
            "revision": 1,
            "recorded_at": utc_now(),
            "schema_version": "pco/source/v1",
            "payload": {
                "source_id": source_id,
                "role": "input",
                "provider": provider,
                "locator": locator,
                "display_name": display_name,
                "reader_skill": reader_skill,
                "snapshot_path": snapshot_path
                or f"sources/snapshots/{source_id}{suffix}",
                "content_hash": None,
                "registered_at": utc_now(),
                "status": "active",
            },
        }
        manager = TransactionManager(
            self.workspace.repository, self.workspace.config.state_root
        )
        state = manager.begin(
            transaction_id=f"txn_source_{uuid.uuid4().hex}",
            fingerprint_context={
                "kind": "source_register",
                "locator": locator,
                "reader": reader_skill,
            },
        )
        manager.append(
            state.id, Operation(op="append", stream="sources", record=record)
        )
        result = manager.commit(state.id)
        return {
            "ok": True,
            "idempotent": False,
            "source_id": source_id,
            "commit": result["commit"],
            "record": record,
        }

    def register_local(
        self, path: Path, *, display_name: str | None = None
    ) -> dict[str, Any]:
        source = path.resolve()
        ensure(
            source.is_file(),
            "SOURCE_NOT_READABLE",
            "source",
            f"Source is not a readable file: {source}",
            value=str(source),
            canonical_changed=False,
            retry_safe=False,
        )
        return self._register_locator(
            source.as_uri(),
            reader_skill="local-readonly",
            provider="local_file",
            display_name=display_name or source.name,
        )

    def register_locator(
        self,
        locator: str,
        *,
        reader_skill: str,
        provider: str = "remote",
        display_name: str | None = None,
        snapshot_path: str | None = None,
    ) -> dict[str, Any]:
        """Register a non-file locator for a reader already in the registry."""

        _validate_locator(locator)
        parsed = urlparse(locator)
        ensure(
            parsed.scheme != "file",
            "SOURCE_USE_REGISTER_LOCAL",
            "source",
            "Use register_local for file locators",
            value=locator,
            canonical_changed=False,
            retry_safe=False,
        )
        return self._register_locator(
            locator,
            reader_skill=reader_skill,
            provider=provider,
            display_name=display_name or locator,
            snapshot_path=snapshot_path,
        )

    def register_remote(
        self,
        locator: str,
        *,
        reader_skill: str,
        provider: str = "remote",
        display_name: str | None = None,
        snapshot_path: str | None = None,
    ) -> dict[str, Any]:
        """Explicit alias for registering a non-file/remote locator."""

        return self.register_locator(
            locator,
            reader_skill=reader_skill,
            provider=provider,
            display_name=display_name,
            snapshot_path=snapshot_path,
        )

    def materialize(self, record: dict[str, Any]) -> SourceRead:
        """Read and validate one source through the wrapper-owned registry."""

        payload = record.get("payload", {})
        locator = _validate_locator(payload.get("locator", ""))
        reader_name = payload.get("reader_skill")
        ensure(
            isinstance(reader_name, str) and reader_name,
            "SOURCE_READER_REQUIRED",
            "source",
            "Source record has no reader_skill",
            record_id=record.get("id"),
            canonical_changed=False,
            retry_safe=False,
        )
        materialized = self.reader_registry.read(reader=reader_name, locator=locator)
        ensure(
            materialized["locator"] == locator,
            "SOURCE_READER_CONTRACT_INVALID",
            "source",
            "Reader returned a different locator",
            record_id=record.get("id"),
            value=materialized["locator"],
            canonical_changed=False,
            retry_safe=False,
        )
        # Readers may normalize for their own API, but the wrapper owns the
        # canonical normalization boundary before hashing and diffing.
        materialized["normalized_content"] = normalize_text(
            materialized["normalized_content"]
        )
        _validate_locator(materialized["locator"])
        return materialized

    # Kept as a thin compatibility alias for callers that used the old
    # private reader boundary in integrations.
    def _read(self, record: dict[str, Any]) -> str:
        return self.materialize(record)["normalized_content"]

    def collect_diffs(self) -> dict[str, Any]:
        current = self.workspace.repository.current_records("sources")
        changes: list[dict[str, Any]] = []
        operations: list[Operation] = []
        source_hashes: dict[str, str] = {}
        for source_id, record in current.items():
            if record["payload"]["status"] != "active":
                continue
            materialized = self.materialize(record)
            content = materialized["normalized_content"]
            digest = content_hash(content)
            source_hashes[source_id] = digest
            if digest == record["payload"].get("content_hash"):
                continue
            snapshot_path = record["payload"]["snapshot_path"]
            previous_path = self.workspace.config.memory_root / snapshot_path
            previous = (
                previous_path.read_text(encoding="utf-8")
                if previous_path.exists()
                else ""
            )
            diff = "".join(
                difflib.unified_diff(
                    previous.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"{source_id}@previous",
                    tofile=f"{source_id}@current",
                )
            )
            updated = {
                **record,
                "revision": record["revision"] + 1,
                "recorded_at": utc_now(),
                "payload": {**record["payload"], "content_hash": digest},
            }
            operations.extend(
                [
                    Operation(op="append", stream="sources", record=updated),
                    Operation(op="write_artifact", path=snapshot_path, content=content),
                ]
            )
            changes.append(
                {
                    "source_id": source_id,
                    "display_name": record["payload"]["display_name"],
                    "old_hash": record["payload"].get("content_hash"),
                    "new_hash": digest,
                    "diff": diff,
                    "snapshot_path": snapshot_path,
                    "source_revision": updated["revision"],
                }
            )
        return {
            "ok": True,
            "changes": changes,
            "operations": operations,
            "source_hashes": source_hashes,
        }


# Descriptive alias for integrations that use the registry as a public PCO
# boundary, while ReaderRegistry remains the concise compatibility name.
SourceReaderRegistry = ReaderRegistry
