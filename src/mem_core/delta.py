from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

from .errors import MemError, ensure
from .models import Operation, RecordEnvelope
from .profile import Profile


def show_bytes(root: Path, commit: str, path: str) -> bytes:
    """Read a path's bytes at a revision; missing path at that revision -> b''."""
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{commit}:{path}"],
        capture_output=True,
        text=False,
        check=False,
    )
    return b"" if result.returncode else result.stdout


def is_messages_only(operations: Iterable[Operation]) -> bool:
    return all(op.op == "append" and op.stream == "messages" for op in operations)


def latest_base_by_id(lines: Iterable[str]) -> tuple[dict[str, dict[str, Any]], bool]:
    """Tolerant latest-by-id over raw JSONL lines.

    Returns (latest, baseline_reliable). Lines that fail to parse, or whose
    id/revision are missing or of the wrong type, are skipped: historical
    corruption is deliberately not surfaced on the hot path (D6). Any skipped
    line marks the baseline unreliable: it could be the latest revision of a
    delta id, so revision-continuity assertions based on the tolerant baseline
    would spuriously reject valid deltas. The caller must then skip those
    assertions for the stream; `mem git verify` remains the strict entry point.
    """
    current: dict[str, dict[str, Any]] = {}
    reliable = True
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            record_id = record["id"]
            revision = record["revision"]
        except (json.JSONDecodeError, KeyError, TypeError):
            reliable = False
            continue
        if not isinstance(record_id, str) or not isinstance(revision, int) or isinstance(revision, bool):
            reliable = False
            continue
        previous = current.get(record_id)
        if previous is None or revision > previous["revision"]:
            current[record_id] = record
    return current, reliable


def validate_delta_records(
    profile: Profile,
    delta: dict[str, list[dict[str, Any]]],
    current: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> int:
    """Envelope + schema (+ revision continuity when current is provided) for delta records only."""
    count = 0
    for stream, records in delta.items():
        stream_current = current.get(stream, {}) if current is not None else None
        for record in records:
            try:
                envelope = RecordEnvelope.model_validate(record)
            except Exception as exc:
                raise MemError("ENVELOPE_INVALID", "envelope_validation", str(exc), stream=stream, record_id=record.get("id")) from exc
            if stream_current is not None:
                expected = stream_current.get(envelope.id, {}).get("revision", 0) + 1
                ensure(envelope.revision == expected, "REVISION_SEQUENCE_INVALID", "revision_validation",
                       f"Expected revision {expected}, got {envelope.revision}", stream=stream,
                       record_id=envelope.id, path="/revision", value=envelope.revision)
                stream_current[envelope.id] = record
            profile.validate_record_schema(stream, record)
            count += 1
    return count
