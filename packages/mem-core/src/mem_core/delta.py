from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

from .errors import MemError, ensure
from .models import Operation, RecordEnvelope, latest_by_id
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


def is_fast_path(profile: Profile, operations: Iterable[Operation]) -> bool:
    """Return whether operations are covered by their Profile stream policies.

    A fast-path transaction is deliberately narrow: it may only append records
    and every affected stream must explicitly opt into delta-only validation
    without cross-record validators.  The stream name is never part of this
    decision.
    """
    operations = list(operations)
    return bool(operations) and all(
        operation.op == "append"
        and operation.stream is not None
        and profile.uses_delta_fast_path(operation.stream)
        for operation in operations
    )


def is_messages_only(
    profile_or_operations: Profile | Iterable[Operation],
    operations: Iterable[Operation] | Profile | None = None,
) -> bool:
    """Compatibility name for the former fast-path predicate.

    New callers should use :func:`is_fast_path`.  The two-argument forms
    support both ``(profile, operations)`` and ``(operations, profile)`` while
    retaining the old one-argument shape as a conservative append-only check.
    The legacy form has no Profile policy available, so it is not used by the
    transaction or hook paths.
    """
    if isinstance(profile_or_operations, Profile):
        ensure(isinstance(operations, Iterable), "VALIDATION_POLICY_REQUIRED", "transaction_validation", "Profile operations are required")
        return is_fast_path(profile_or_operations, operations)
    if isinstance(operations, Profile):
        return is_fast_path(operations, profile_or_operations)
    if operations is not None:
        raise TypeError("is_messages_only expects (profile, operations) or (operations, profile)")
    return bool(profile_or_operations) and all(operation.op == "append" for operation in profile_or_operations)


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
        # A missing stream entry means its tolerant historical baseline was
        # unreliable; validate its delta without making a possibly false
        # revision-continuity assertion.  Full validation remains strict.
        stream_current = current.get(stream) if current is not None and stream in current else None
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


def validate_structured_delta(
    *,
    profile: Profile,
    base_records: dict[str, list[dict[str, Any]]],
    delta: dict[str, list[dict[str, Any]]],
    root: Path,
) -> int:
    """Validate a structured transaction against base + delta records.

    Historical records are strict-envelope checked only (cheap), delta records
    get envelope + schema + revision continuity, and Profile Validators run on
    the merged base + delta view so cross-record references behave exactly like
    a full validation of the materialized staged tree.
    """
    base_current: dict[str, dict[str, dict[str, Any]]] = {}
    for stream in profile.config.streams:
        records = base_records.get(stream, [])
        for record in records:
            try:
                RecordEnvelope.model_validate(record)
            except Exception as exc:
                raise MemError(
                    "ENVELOPE_INVALID",
                    "envelope_validation",
                    str(exc),
                    stream=stream,
                    record_id=record.get("id"),
                ) from exc
        base_current[stream] = latest_by_id(records)
    count = validate_delta_records(profile, delta, base_current)
    merged = {
        stream: base_records.get(stream, []) + delta.get(stream, [])
        for stream in profile.config.streams
    }
    profile.run_validators(root, merged)
    return count
