"""Framed, domain-separated identities for reviewed proposal commits."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
import json
from typing import Any

from mem_core.models import Operation

from .markdown import ParsedBusinessObject
from .materials import CapturedMaterial
from .models import LocalFileMaterial, ProposalArtifactMaterial


_OBJECT_TYPES = {
    "evidence": "evidence",
    "sources": "source",
    "events": "event",
    "psychologies": "psychology",
    "philosophies": "philosophy",
    "archetypes": "archetype",
    "hypotheses": "hypothesis",
    "meta_revisions": "meta",
    "session_handoffs": "session_handoff",
}


def frame(value: bytes | str) -> bytes:
    """Encode one opaque byte string with an unsigned 64-bit byte length."""

    if isinstance(value, str):
        data = value.encode("utf-8")
    elif isinstance(value, bytes):
        data = value
    else:
        raise TypeError("framed values must be bytes or strings")
    if len(data) >= 1 << 64:
        raise ValueError("framed value exceeds the unsigned 64-bit length domain")
    return len(data).to_bytes(8, "big", signed=False) + data


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _finish(hasher: "hashlib._Hash") -> str:
    return "sha256:" + hasher.hexdigest()


def receipt_id(workspace_id: str, review_id: str) -> str:
    digest = hashlib.sha256(
        b"pco-v0.5-proposal-commit\0" + frame(workspace_id) + frame(review_id)
    ).hexdigest()
    return "commit_" + digest


def input_digest(materials: Iterable[CapturedMaterial]) -> str:
    """Identify captured descriptors and normalized bytes, independent of order."""

    captured = sorted(materials, key=lambda item: item.material_id.encode("utf-8"))
    identifiers = [item.material_id for item in captured]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("input material IDs must be unique")
    hasher = hashlib.sha256(b"pco-input-digest-v0.5\0")
    for item in captured:
        descriptor = item.material
        if isinstance(descriptor, LocalFileMaterial):
            selector_name, selector_value = "locator", descriptor.locator
        elif isinstance(descriptor, ProposalArtifactMaterial):
            selector_name, selector_value = "path", descriptor.path.as_posix()
        else:  # defensive for callers bypassing the manifest type adapter
            raise TypeError("unsupported input material descriptor")
        for value in (
            item.material_id,
            item.kind,
            selector_name,
            selector_value,
            descriptor.fidelity,
        ):
            hasher.update(frame(value))
        hasher.update(frame(item.normalized_bytes))
    return _finish(hasher)


def proposal_digest(items: Iterable[ParsedBusinessObject]) -> str:
    """Identify only parsed business semantics in deterministic path order."""

    parsed = sorted(items, key=lambda item: item.relative_path.as_posix().encode("utf-8"))
    paths = [item.relative_path.as_posix() for item in parsed]
    if len(paths) != len(set(paths)):
        raise ValueError("proposal business paths must be unique")
    hasher = hashlib.sha256(b"pco-proposal-digest-v0.5\0")
    for item in parsed:
        values: tuple[bytes | str, ...] = (
            _OBJECT_TYPES[item.stream],
            item.operation,
            item.record_id,
            _canonical_json(item.base_revision),
            _canonical_json(item.model_dump(mode="json")["structured"]),
            item.body_markdown,
        )
        for value in values:
            hasher.update(frame(value))
    return _finish(hasher)


def _operation_identity(raw: Operation | Mapping[str, Any]) -> dict[str, Any]:
    operation = raw if isinstance(raw, Operation) else Operation.model_validate(raw)
    normalized = operation.model_dump(mode="json", exclude_none=True)
    if operation.op == "append":
        record = dict(normalized["record"])
        record.pop("recorded_at", None)
        normalized["record"] = record
    else:
        content = normalized.pop("content").encode("utf-8")
        normalized["content_hash"] = (
            "sha256:" + hashlib.sha256(content).hexdigest()
        )
    return normalized


def transaction_digest(
    *,
    workspace_id: str,
    base_commit: str,
    input_digest_value: str,
    proposal_digest_value: str,
    manifest_hash: str,
    receipt_id_value: str,
    profile_version: str,
    manifest_schema_version: str,
    stream_schema_versions: Mapping[str, str],
    operations: Iterable[Operation | Mapping[str, Any]],
) -> str:
    """Bind canonical controls and prospective operations without timestamps."""

    hasher = hashlib.sha256(b"pco-transaction-digest-v0.5\0")
    for value in (
        workspace_id,
        base_commit,
        input_digest_value,
        proposal_digest_value,
        manifest_hash,
        receipt_id_value,
        profile_version,
        manifest_schema_version,
        _canonical_json(dict(stream_schema_versions)),
        _canonical_json([_operation_identity(item) for item in operations]),
    ):
        hasher.update(frame(value))
    return _finish(hasher)
