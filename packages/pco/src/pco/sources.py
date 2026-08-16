from __future__ import annotations

import difflib
import hashlib
import mimetypes
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, TypedDict
from urllib.parse import unquote, urlparse

from mem_core.errors import MemError, ensure
from mem_core.models import Operation, utc_now
from mem_core.transaction import TransactionManager

from .workspace import Workspace


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
    ensure(isinstance(locator, str) and locator.strip(), "SOURCE_LOCATOR_INVALID", "source", "Source locator must be a non-empty string")
    parsed = urlparse(locator)
    ensure(parsed.scheme and parsed.scheme not in {"data", "javascript"}, "SOURCE_LOCATOR_INVALID", "source", "Source locator must use a supported URI scheme", value=locator)
    if parsed.scheme == "file":
        ensure(parsed.path, "SOURCE_LOCATOR_INVALID", "source", "File locator must include a path", value=locator)
    elif parsed.scheme in {"http", "https"}:
        ensure(parsed.netloc, "SOURCE_LOCATOR_INVALID", "source", "HTTP(S) locator must include a host", value=locator)
    return locator


def read_local_file(locator: str) -> SourceRead:
    """Built-in read-only reader for ``file:`` locators."""

    _validate_locator(locator)
    parsed = urlparse(locator)
    ensure(parsed.scheme == "file", "SOURCE_READER_LOCATOR_MISMATCH", "source", "The local reader only accepts file locators", value=locator)
    path = Path(unquote(parsed.path))
    ensure(path.is_file(), "SOURCE_NOT_READABLE", "source", f"Registered source is unavailable: {path}", value=str(path))
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
            reader = target if callable(target) else profile.registry.resolve(str(target))
            result.register(str(name), reader)
        return result

    def register(self, name: str, reader: SourceReader) -> None:
        ensure(isinstance(name, str) and name.strip(), "SOURCE_READER_INVALID", "source", "Reader name must be non-empty")
        ensure(callable(reader), "SOURCE_READER_INVALID", "source", f"Reader is not callable: {name}", value=name)
        ensure(name not in self._readers, "SOURCE_READER_DUPLICATE", "source", f"Reader is already registered: {name}", value=name)
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
                retryable=True,
                recovery=["Register the source reader before materializing the source"],
            ) from exc

    def read(self, *, reader: str, locator: str) -> SourceRead:
        result = self.resolve(reader)(locator)
        ensure(isinstance(result, Mapping), "SOURCE_READER_CONTRACT_INVALID", "source", "Source reader must return a mapping", value=reader)
        required = {"locator", "reader", "normalized_content", "media_type", "read_metadata"}
        ensure(set(result) == required, "SOURCE_READER_CONTRACT_INVALID", "source", "Source reader returned fields outside the materialization contract", value={"reader": reader, "fields": sorted(result)})
        ensure(isinstance(result["locator"], str), "SOURCE_READER_CONTRACT_INVALID", "source", "Reader locator must be a string", value=reader)
        ensure(isinstance(result["reader"], str) and result["reader"] == reader, "SOURCE_READER_CONTRACT_INVALID", "source", "Reader must identify itself with its registry name", value=reader)
        ensure(isinstance(result["normalized_content"], str), "SOURCE_READER_CONTRACT_INVALID", "source", "Reader content must be text", value=reader)
        ensure(isinstance(result["media_type"], str) and result["media_type"].strip(), "SOURCE_READER_CONTRACT_INVALID", "source", "Reader media_type must be non-empty", value=reader)
        ensure(isinstance(result["read_metadata"], Mapping), "SOURCE_READER_CONTRACT_INVALID", "source", "Reader read_metadata must be a mapping", value=reader)
        return {
            "locator": result["locator"],
            "reader": result["reader"],
            "normalized_content": result["normalized_content"],
            "media_type": result["media_type"],
            "read_metadata": dict(result["read_metadata"]),
        }


class SourceManager:
    def __init__(self, workspace: Workspace, reader_registry: ReaderRegistry | None = None) -> None:
        self.workspace = workspace
        self.reader_registry = reader_registry or ReaderRegistry.from_profile(workspace.profile)

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
            if record["payload"].get("locator") == locator and record["payload"].get("status") == "active":
                return {
                    "ok": True,
                    "idempotent": True,
                    "source_id": record["id"],
                    "commit": None,
                    "record": record,
                }
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
                "snapshot_path": snapshot_path or f"sources/snapshots/{source_id}{suffix}",
                "content_hash": None,
                "registered_at": utc_now(),
                "status": "active",
            },
        }
        manager = TransactionManager(self.workspace.repository, self.workspace.config.state_root)
        state = manager.begin(
            transaction_id=f"txn_source_{uuid.uuid4().hex}",
            fingerprint_context={"kind": "source_register", "locator": locator, "reader": reader_skill},
        )
        manager.append(state.id, Operation(op="append", stream="sources", record=record))
        result = manager.commit(state.id)
        return {"ok": True, "source_id": source_id, "commit": result["commit"], "record": record}

    def register_local(self, path: Path, *, display_name: str | None = None) -> dict[str, Any]:
        source = path.resolve()
        ensure(source.is_file(), "SOURCE_NOT_READABLE", "source", f"Source is not a readable file: {source}", value=str(source))
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
        ensure(parsed.scheme != "file", "SOURCE_USE_REGISTER_LOCAL", "source", "Use register_local for file locators", value=locator)
        self.reader_registry.resolve(reader_skill)
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
        ensure(isinstance(reader_name, str) and reader_name, "SOURCE_READER_REQUIRED", "source", "Source record has no reader_skill", record_id=record.get("id"))
        materialized = self.reader_registry.read(reader=reader_name, locator=locator)
        ensure(materialized["locator"] == locator, "SOURCE_READER_CONTRACT_INVALID", "source", "Reader returned a different locator", record_id=record.get("id"), value=materialized["locator"])
        # Readers may normalize for their own API, but the wrapper owns the
        # canonical normalization boundary before hashing and diffing.
        materialized["normalized_content"] = normalize_text(materialized["normalized_content"])
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
            previous = previous_path.read_text(encoding="utf-8") if previous_path.exists() else ""
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
        return {"ok": True, "changes": changes, "operations": operations, "source_hashes": source_hashes}


# Descriptive alias for integrations that use the registry as a public PCO
# boundary, while ReaderRegistry remains the concise compatibility name.
SourceReaderRegistry = ReaderRegistry
