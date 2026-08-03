from __future__ import annotations

import difflib
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from mem_core.errors import MemError, ensure
from mem_core.models import Operation
from mem_core.transaction import TransactionManager

from .workspace import Workspace


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def content_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


class SourceManager:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def register_local(self, path: Path, *, display_name: str | None = None) -> dict[str, Any]:
        source = path.resolve()
        ensure(source.is_file(), "SOURCE_NOT_READABLE", "source", f"Source is not a readable file: {source}", value=str(source))
        locator = source.as_uri()
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
        record = {
            "id": source_id,
            "revision": 1,
            "recorded_at": _now(),
            "schema_version": "pco/source/v1",
            "payload": {
                "source_id": source_id,
                "role": "input",
                "provider": "local_file",
                "locator": locator,
                "display_name": display_name or source.name,
                "reader_skill": "local-readonly",
                "snapshot_path": f"sources/snapshots/{source_id}{source.suffix or '.md'}",
                "content_hash": None,
                "registered_at": _now(),
                "status": "active",
            },
        }
        manager = TransactionManager(self.workspace.repository, self.workspace.config.state_root)
        state = manager.begin(
            transaction_id=f"txn_source_{uuid.uuid4().hex}",
            fingerprint_context={"kind": "source_register", "locator": locator},
        )
        manager.append(state.id, Operation(op="append", stream="sources", record=record))
        result = manager.commit(state.id)
        return {"ok": True, "source_id": source_id, "commit": result["commit"], "record": record}

    def _read(self, record: dict[str, Any]) -> str:
        locator = record["payload"]["locator"]
        parsed = urlparse(locator)
        if parsed.scheme != "file":
            raise MemError(
                "SOURCE_READER_REQUIRED",
                "source",
                f"No built-in reader for {parsed.scheme}; use {record['payload']['reader_skill']}",
                record_id=record["id"],
                retryable=True,
            )
        path = Path(unquote(parsed.path))
        ensure(path.is_file(), "SOURCE_NOT_READABLE", "source", f"Registered source is unavailable: {path}", record_id=record["id"], value=str(path))
        return normalize_text(path.read_text(encoding="utf-8"))

    def collect_diffs(self) -> dict[str, Any]:
        current = self.workspace.repository.current_records("sources")
        changes: list[dict[str, Any]] = []
        operations: list[Operation] = []
        source_hashes: dict[str, str] = {}
        for source_id, record in current.items():
            if record["payload"]["status"] != "active":
                continue
            content = self._read(record)
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
                "recorded_at": _now(),
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
                }
            )
        return {"ok": True, "changes": changes, "operations": operations, "source_hashes": source_hashes}
