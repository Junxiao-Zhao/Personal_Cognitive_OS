from __future__ import annotations

import difflib
import json
import re
import subprocess
from typing import Any, Mapping

from mem_core.errors import MemError

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
