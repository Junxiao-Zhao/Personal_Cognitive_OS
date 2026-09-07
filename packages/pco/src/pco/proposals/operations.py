"""Translate one validated immutable proposal into a mem-core application request."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import subprocess
from typing import Any

from mem_core.errors import MemError
from mem_core.delta import validate_structured_delta
from mem_core.application import application_transaction_id
from mem_core.models import ApplicationCommitRequest, Operation, latest_by_id, utc_now

from .digests import input_digest, proposal_digest, receipt_id, transaction_digest
from .markdown import parse_business_markdown
from .materials import CapturedMaterial, digest
from .models import (
    LocalFileMaterial,
    ProposalArtifactMaterial,
    ProposalCommitReceipt,
    ProposalTransactionLine,
)
from ..config import PROPOSAL_MANIFEST_VERSION
from .snapshot import ProposalSnapshot, snapshot_identity
from .validation import ValidationResult


@dataclass(frozen=True, slots=True, init=False)
class SemanticNoOp:
    """A matching prior proposal whose target records are already effective."""

    input_digest: str
    proposal_digest: str
    _equivalent_json: tuple[bytes, ...]
    _warnings_json: tuple[bytes, ...]

    def __init__(
        self,
        input_digest: str,
        proposal_digest: str,
        equivalent_records: tuple[dict[str, Any], ...],
    ) -> None:
        object.__setattr__(self, "input_digest", input_digest)
        object.__setattr__(self, "proposal_digest", proposal_digest)
        object.__setattr__(
            self,
            "_equivalent_json",
            tuple(_canonical_json(record) for record in equivalent_records),
        )
        object.__setattr__(
            self,
            "_warnings_json",
            (
                _canonical_json(
                    {
                        "code": "INPUT_REPLAY",
                        "message": "The captured input material matches a prior proposal commit",
                    }
                ),
            ),
        )

    @property
    def equivalent_records(self) -> tuple[dict[str, Any], ...]:
        return tuple(json.loads(value) for value in self._equivalent_json)

    @property
    def warnings(self) -> tuple[dict[str, Any], ...]:
        return tuple(json.loads(value) for value in self._warnings_json)

    def as_result(self) -> dict[str, Any]:
        return {
            "ok": True,
            "canonical_status": "no_op",
            "input_digest": self.input_digest,
            "proposal_digest": self.proposal_digest,
            "equivalent_records": list(self.equivalent_records),
            "warnings": list(self.warnings),
        }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _error(code: str, message: str, *, path: str | None = None, value: Any = None) -> MemError:
    return MemError(
        code,
        "proposal_operations",
        message,
        path=path,
        value=value,
        recovery=["Restore canonical memory from a trusted copy and validate the proposal again"],
        canonical_changed=False,
        retry_safe=False,
    )


def _git_object(
    workspace: Any, base_commit: str, relative: str, *, required: bool
) -> bytes | None:
    spec = f"{base_commit}:{relative}"
    try:
        exists = subprocess.run(
            ["git", "-C", str(workspace.repository.root), "cat-file", "-e", spec],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise _error("WORKSPACE_INTEGRITY_ERROR", "Canonical Git object cannot be inspected", path=relative, value=str(exc)) from exc
    if exists.returncode:
        if required:
            raise _error("WORKSPACE_INTEGRITY_ERROR", "Required canonical file is missing from the proposal base", path=relative)
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace.repository.root), "show", spec],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise _error("WORKSPACE_INTEGRITY_ERROR", "Canonical Git object cannot be read", path=relative, value=str(exc)) from exc
    if result.returncode:
        raise _error("WORKSPACE_INTEGRITY_ERROR", "Canonical Git object cannot be read", path=relative, value=result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _operation_path(operation: Operation) -> str:
    if operation.op == "write_artifact":
        assert operation.path is not None and operation.content is not None
        if not operation.path.startswith("source_blobs/"):
            raise ValueError("proposal artifacts must be source blobs")
        suffix = operation.path.removeprefix("source_blobs/")
        if len(suffix) != 64 or any(char not in "0123456789abcdef" for char in suffix):
            raise ValueError("source blob path must contain one lowercase SHA-256 hex")
        if digest(operation.content.encode("utf-8")) != "sha256:" + suffix:
            raise ValueError("source blob path does not match its UTF-8 content")
        return operation.path
    assert operation.stream is not None and operation.record is not None
    record_id = operation.record.get("id")
    if not isinstance(record_id, str) or not record_id or "/" in record_id or "\\" in record_id:
        raise ValueError("proposal record ID cannot be mapped to one business path")
    if operation.stream == "meta_revisions":
        return "META_MEMORY.md"
    if operation.stream == "session_handoffs":
        return "SESSION_HANDOFF.md"
    directories = {
        "evidence": "evidence",
        "sources": "sources",
        "events": "events",
        "psychologies": "psychologies",
        "philosophies": "philosophies",
        "archetypes": "archetypes",
        "hypotheses": "hypotheses",
    }
    if operation.stream not in directories:
        raise ValueError("proposal append targets an unknown business stream")
    return f"{directories[operation.stream]}/{record_id}.md"


def _operation_order_key(operation: Operation) -> tuple[int, bytes]:
    if operation.op == "write_artifact":
        rank = 0
    elif operation.stream == "sources":
        rank = 1
    else:
        rank = 2
    return rank, _operation_path(operation).encode("utf-8")


def _validate_stored_operations(
    workspace: Any,
    raw_operations: Any,
    operations: list[Operation],
) -> None:
    if not isinstance(raw_operations, list) or len(raw_operations) != len(operations):
        raise ValueError("proposal operations must be one JSON array")
    if any(
        not isinstance(raw, dict) or raw != operation.normalized()
        for raw, operation in zip(raw_operations, operations, strict=True)
    ):
        raise ValueError("proposal operations must use the canonical normalized shape")
    if operations != sorted(operations, key=_operation_order_key):
        raise ValueError("proposal operations are not in canonical source/business order")
    paths: set[str] = set()
    records: set[tuple[str, str]] = set()
    for operation in operations:
        _operation_path(operation)
        if operation.op == "write_artifact":
            assert operation.path is not None
            if operation.path in paths:
                raise ValueError("proposal source blob operations must be unique")
            paths.add(operation.path)
            continue
        assert operation.stream is not None and operation.record is not None
        workspace.profile.validate_record_schema(operation.stream, operation.record)
        key = (operation.stream, operation.record["id"])
        if key in records:
            raise ValueError("proposal business append targets must be unique")
        records.add(key)
        if operation.stream == "sources":
            payload = operation.record["payload"]
            content_hash = payload.get("content_hash")
            if payload.get("snapshot_ref") != "source_blobs/" + str(
                content_hash
            ).removeprefix("sha256:"):
                raise ValueError("source revision snapshot_ref does not match content_hash")


def _transactions(workspace: Any, base_commit: str) -> tuple[dict[str, Any], ...]:
    raw = _git_object(
        workspace, base_commit, "transactions/transactions.jsonl", required=True
    )
    assert raw is not None
    try:
        text = raw.decode("utf-8", errors="strict")
        records = tuple(
            json.loads(
                line,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_nonfinite,
            )
            for line in text.splitlines()
            if line.strip()
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _error("WORKSPACE_INTEGRITY_ERROR", "Canonical transaction ledger is invalid", value=str(exc)) from exc
    if not all(isinstance(record, dict) for record in records):
        raise _error("WORKSPACE_INTEGRITY_ERROR", "Canonical transaction ledger contains a non-object")
    transaction_ids: set[str] = set()
    fingerprints: set[str] = set()
    receipt_ids: set[str] = set()
    verified: list[dict[str, Any]] = []
    try:
        for record in records:
            transaction_id = record.get("id")
            fingerprint = record.get("transaction_fingerprint")
            if isinstance(transaction_id, str):
                if transaction_id in transaction_ids:
                    raise ValueError("duplicate canonical transaction ID")
                transaction_ids.add(transaction_id)
            if isinstance(fingerprint, str):
                if fingerprint in fingerprints:
                    raise ValueError("duplicate canonical transaction fingerprint")
                fingerprints.add(fingerprint)
            receipt_value = record.get("receipt")
            is_proposal = record.get("application_policy") == "pco_proposal" or (
                isinstance(receipt_value, dict)
                and receipt_value.get("type") == "proposal_commit_receipt"
            )
            if not is_proposal:
                continue
            transaction = ProposalTransactionLine.model_validate(record)
            receipt = transaction.receipt
            if receipt.receipt_id in receipt_ids:
                raise ValueError("duplicate canonical proposal receipt ID")
            receipt_ids.add(receipt.receipt_id)
            if transaction.operation_count != len(transaction.operations):
                raise ValueError("proposal operation_count does not match operations")
            if transaction.id != application_transaction_id(
                transaction.transaction_fingerprint
            ):
                raise ValueError("proposal transaction ID is not digest-derived")
            if receipt.workspace_id != workspace.identity["workspace_id"]:
                raise ValueError("proposal receipt belongs to another workspace")
            if transaction.application_policy != workspace.profile.config.application_commit_policy:
                raise ValueError("proposal transaction policy does not match the Profile")
            if receipt.receipt_id != receipt_id(
                receipt.workspace_id, receipt.review_id
            ):
                raise ValueError("proposal receipt ID is not workspace/review derived")
            if receipt.transaction_digest != transaction.transaction_fingerprint:
                raise ValueError("proposal receipt digest does not match its transaction")
            _validate_stored_operations(
                workspace, record.get("operations"), transaction.operations
            )
            # v0.5 receipts intentionally omit their manifest schema version.
            # This verifier is therefore deliberately limited to the current
            # v0.5 manifest contract rather than guessing a historical value.
            recomputed = transaction_digest(
                workspace_id=receipt.workspace_id,
                base_commit=transaction.base_commit,
                input_digest_value=receipt.input_digest,
                proposal_digest_value=receipt.proposal_digest,
                manifest_hash=receipt.manifest_hash,
                receipt_id_value=receipt.receipt_id,
                profile_version=workspace.profile.version,
                manifest_schema_version=PROPOSAL_MANIFEST_VERSION,
                stream_schema_versions={
                    name: config.schema_version
                    for name, config in workspace.profile.config.streams.items()
                },
                operations=transaction.operations,
            )
            if recomputed != transaction.transaction_fingerprint:
                raise ValueError("proposal transaction digest cannot be recomputed")
            verified.append(transaction.model_dump(mode="json"))
    except (KeyError, TypeError, ValueError) as exc:
        raise _error(
            "WORKSPACE_INTEGRITY_ERROR",
            "Canonical proposal transaction is malformed or unbound",
            value=str(exc),
        ) from exc
    return tuple(verified)


def _matching_receipts(
    workspace: Any, base_commit: str, input_value: str, proposal_value: str | None
) -> tuple[dict[str, Any], ...]:
    matches: list[dict[str, Any]] = []
    for transaction in _transactions(workspace, base_commit):
        receipt = transaction.get("receipt")
        if not isinstance(receipt, dict):
            continue
        if receipt.get("type") == "proposal_commit_receipt":
            try:
                receipt = ProposalCommitReceipt.model_validate(receipt).model_dump(
                    mode="json"
                )
            except (TypeError, ValueError) as exc:
                raise _error(
                    "WORKSPACE_INTEGRITY_ERROR",
                    "Canonical proposal receipt is invalid",
                    value=str(exc),
                ) from exc
        else:
            continue
        expected_receipt_id = receipt_id(
            receipt["workspace_id"], receipt["review_id"]
        )
        if (
            receipt["receipt_id"] != expected_receipt_id
            or receipt["transaction_digest"]
            != transaction.get("transaction_fingerprint")
            or transaction.get("application_policy") != "pco_proposal"
        ):
            raise _error(
                "WORKSPACE_INTEGRITY_ERROR",
                "Canonical proposal receipt does not match its transaction identity",
            )
        if receipt.get("workspace_id") != workspace.identity["workspace_id"]:
            continue
        if receipt.get("input_digest") != input_value:
            continue
        if proposal_value is not None and receipt.get("proposal_digest") != proposal_value:
            continue
        matches.append(receipt)
    return tuple(matches)


def replay_warnings(
    workspace: Any, snapshot: ProposalSnapshot
) -> tuple[dict[str, Any], ...]:
    """Report prior input use without treating it as a semantic no-op."""

    value = input_digest(snapshot.input_material)
    if not _matching_receipts(workspace, snapshot.manifest.base_commit, value, None):
        return ()
    return (
        {
            "code": "INPUT_REPLAY",
            "message": "The captured input material matches a prior proposal commit",
            "input_digest": value,
        },
    )


def proposal_history(
    workspace: Any,
    snapshot: ProposalSnapshot,
    parsed: tuple[Any, ...],
    base_records: dict[str, list[dict[str, Any]]],
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Return replay warnings and a fully proven semantic-no-op candidate."""

    input_value = input_digest(snapshot.input_material)
    proposal_value = proposal_digest(parsed)
    input_matches = _matching_receipts(
        workspace, snapshot.manifest.base_commit, input_value, None
    )
    if not input_matches:
        return (), ()
    warnings = (
        {
            "code": "INPUT_REPLAY",
            "message": "The captured input material matches a prior proposal commit",
            "input_digest": input_value,
        },
    )
    if not _matching_receipts(
        workspace,
        snapshot.manifest.base_commit,
        input_value,
        proposal_value,
    ):
        return warnings, ()

    by_material = {item.material_id: item for item in snapshot.input_material}
    current = {
        stream: latest_by_id(records) for stream, records in base_records.items()
    }
    equivalent: list[dict[str, Any]] = []
    for item in parsed:
        proposed = item.to_record_preview()
        if item.stream == "sources":
            material = by_material.get(item.material_id or "")
            if material is None:
                return warnings, ()
            proposed["payload"]["content_hash"] = material.content_hash
            proposed["payload"]["snapshot_ref"] = (
                "source_blobs/" + material.content_hash.removeprefix("sha256:")
            )
        effective = current.get(item.stream, {}).get(item.record_id)
        if effective is None or _without_generated_metadata(
            effective
        ) != _without_generated_metadata(proposed):
            return warnings, ()
        workspace.profile.validate_record_schema(item.stream, effective)
        if item.stream == "sources":
            content_hash = proposed["payload"]["content_hash"]
            relative = "source_blobs/" + content_hash.removeprefix("sha256:")
            existing = _git_object(
                workspace,
                snapshot.manifest.base_commit,
                relative,
                required=False,
            )
            if existing is None or digest(existing) != content_hash:
                raise _error(
                    "SOURCE_SNAPSHOT_INVALID",
                    "Semantically equivalent source record lacks its valid canonical snapshot",
                    path=relative,
                    value={
                        "expected": content_hash,
                        "observed": None if existing is None else digest(existing),
                    },
                )
        equivalent.append(effective)

    validate_structured_delta(
        profile=workspace.profile,
        base_records=base_records,
        delta={stream: [] for stream in workspace.profile.config.streams},
        root=workspace.repository.root,
    )
    return warnings, tuple(equivalent)


def _without_generated_metadata(record: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(_canonical_json(record))
    result.pop("recorded_at", None)
    result.pop("commit_ref", None)
    return result


def _receipt_material(material: CapturedMaterial) -> dict[str, Any]:
    common = {
        "material_id": material.material_id,
        "kind": material.kind,
        "hash": material.content_hash,
    }
    descriptor = material.material
    if isinstance(descriptor, LocalFileMaterial):
        return {**common, "locator": descriptor.locator}
    if isinstance(descriptor, ProposalArtifactMaterial):
        if material.file_hash is None:
            raise _error("PROPOSAL_SNAPSHOT_INCOMPLETE", "Proposal artifact lacks its exact file hash", path=descriptor.path.as_posix())
        return {
            **common,
            "path": descriptor.path.as_posix(),
            "file_hash": material.file_hash,
        }
    raise TypeError("unsupported captured material")


def _verified_snapshot_values(
    snapshot: ProposalSnapshot, validated: ValidationResult
) -> tuple[tuple[CapturedMaterial, ...], dict[str, str]]:
    reparsed = tuple(
        parse_business_markdown(item.path, item.data)
        for item in snapshot.business_files
    )
    if tuple(_canonical_json(item.model_dump(mode="json")) for item in reparsed) != tuple(
        _canonical_json(item.model_dump(mode="json")) for item in validated.parsed
    ):
        raise _error(
            "PROPOSAL_SNAPSHOT_INCOMPLETE",
            "Validation result does not belong to the immutable proposal snapshot",
        )
    file_hashes: dict[str, str] = {}
    for item in snapshot.business_files:
        observed = digest(item.data)
        if observed != item.file_hash:
            raise _error(
                "PROPOSAL_SNAPSHOT_INCOMPLETE",
                "Captured business file hash does not match its immutable bytes",
                path=item.path.as_posix(),
            )
        file_hashes[item.path.as_posix()] = observed
    materials: list[CapturedMaterial] = []
    for item in snapshot.input_material:
        observed_content = digest(item.normalized_bytes)
        if observed_content != item.content_hash:
            raise _error(
                "PROPOSAL_SNAPSHOT_INCOMPLETE",
                "Captured input material hash does not match its normalized bytes",
                value={"material_id": item.material_id},
            )
        observed_file: str | None = None
        if isinstance(item.material, ProposalArtifactMaterial):
            observed_file = digest(item.raw_bytes)
            if item.file_hash != observed_file:
                raise _error(
                    "PROPOSAL_SNAPSHOT_INCOMPLETE",
                    "Captured proposal artifact hash does not match its exact bytes",
                    path=item.material.path.as_posix(),
                )
        elif item.file_hash is not None:
            raise _error(
                "PROPOSAL_SNAPSHOT_INCOMPLETE",
                "External local input must not carry a proposal file hash",
                value={"material_id": item.material_id},
            )
        materials.append(
            replace(
                item,
                content_hash=observed_content,
                file_hash=observed_file,
            )
        )
    return tuple(materials), file_hashes


def build_commit_request(
    workspace: Any,
    snapshot: ProposalSnapshot,
    validated: ValidationResult,
) -> ApplicationCommitRequest | SemanticNoOp:
    """Build one deterministic transaction from exactly the validated snapshot."""

    manifest = snapshot.manifest
    if validated.snapshot_identity != snapshot_identity(snapshot):
        raise _error(
            "PROPOSAL_SNAPSHOT_INCOMPLETE",
            "Validation result does not belong to the complete immutable proposal snapshot",
        )
    captured_materials, business_file_hashes = _verified_snapshot_values(
        snapshot, validated
    )
    workspace_id = workspace.identity["workspace_id"]
    commit_reference = receipt_id(workspace_id, manifest.review_id)
    input_value = input_digest(captured_materials)
    proposal_value = proposal_digest(validated.parsed)
    manifest_hash = digest(snapshot.manifest_bytes)
    if validated.is_semantic_noop:
        if not _matching_receipts(
            workspace,
            manifest.base_commit,
            input_value,
            proposal_value,
        ):
            raise _error(
                "WORKSPACE_INTEGRITY_ERROR",
                "Validated semantic no-op has no exact prior proposal transaction",
            )
        return SemanticNoOp(
            input_value,
            proposal_value,
            validated.equivalent_records,
        )
    generated_at = utc_now()

    if len(validated.parsed) != len(validated.revisions):
        raise _error("PROPOSAL_SNAPSHOT_INCOMPLETE", "Validated parsed objects and revision previews differ")
    by_material = {item.material_id: item for item in captured_materials}
    records: list[tuple[str, dict[str, Any]]] = []
    source_materials: dict[str, CapturedMaterial] = {}
    for parsed, preview in zip(validated.parsed, validated.revisions, strict=True):
        if parsed.stream != preview.stream or parsed.record_id != preview.record["id"]:
            raise _error("PROPOSAL_SNAPSHOT_INCOMPLETE", "Validated parsed objects and revision previews are misaligned")
        if preview.record != parsed.to_record_preview():
            raise _error(
                "PROPOSAL_SNAPSHOT_INCOMPLETE",
                "Validated revision preview does not match parsed proposal semantics",
                path=parsed.relative_path.as_posix(),
            )
        record = preview.record
        record["recorded_at"] = generated_at
        record["commit_ref"] = commit_reference
        if parsed.stream == "sources":
            material = by_material.get(parsed.material_id or "")
            if material is None:
                raise _error("PROPOSAL_MATERIAL_MISMATCH", "A proposed source lacks captured material", path=parsed.relative_path.as_posix())
            record["payload"]["content_hash"] = material.content_hash
            record["payload"]["snapshot_ref"] = "source_blobs/" + material.content_hash.removeprefix("sha256:")
            source_materials[material.content_hash] = material
        workspace.profile.validate_record_schema(parsed.stream, record)
        records.append((parsed.stream, record))

    blob_operations: list[Operation] = []
    for content_hash in sorted(source_materials, key=lambda value: value.encode("utf-8")):
        material = source_materials[content_hash]
        relative = "source_blobs/" + content_hash.removeprefix("sha256:")
        existing = _git_object(
            workspace, manifest.base_commit, relative, required=False
        )
        if existing is not None:
            if digest(existing) != content_hash:
                raise _error("SOURCE_SNAPSHOT_INVALID", "Existing content-addressed source blob has invalid bytes", path=relative, value={"expected": content_hash, "observed": digest(existing)})
            continue
        try:
            content = material.normalized_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise _error("SOURCE_SNAPSHOT_INVALID", "Captured source snapshot is not UTF-8", path=relative) from exc
        blob_operations.append(
            Operation(op="write_artifact", path=relative, content=content)
        )

    append_operations = [
        Operation(op="append", stream=stream, record=record)
        for stream, record in records
    ]
    append_operations.sort(
        key=lambda operation: (
            0 if operation.stream == "sources" else 1,
            next(
                item.relative_path.as_posix().encode("utf-8")
                for item in validated.parsed
                if item.stream == operation.stream
                and item.record_id == operation.record["id"]
            ),
        )
    )
    operations = blob_operations + append_operations
    if not operations:
        raise _error("TRANSACTION_EMPTY", "Proposal creates no canonical operation")

    transaction_value = transaction_digest(
        workspace_id=workspace_id,
        base_commit=manifest.base_commit,
        input_digest_value=input_value,
        proposal_digest_value=proposal_value,
        manifest_hash=manifest_hash,
        receipt_id_value=commit_reference,
        profile_version=workspace.profile.version,
        manifest_schema_version=manifest.schema_version,
        stream_schema_versions={
            name: config.schema_version
            for name, config in workspace.profile.config.streams.items()
        },
        operations=operations,
    )
    receipt = ProposalCommitReceipt(
        receipt_id=commit_reference,
        workspace_id=workspace_id,
        review_id=manifest.review_id,
        manifest_hash=manifest_hash,
        files=[
            {
                "path": item.path.as_posix(),
                "hash": business_file_hashes[item.path.as_posix()],
            }
            for item in sorted(
                snapshot.business_files,
                key=lambda value: value.path.as_posix().encode("utf-8"),
            )
        ],
        input_material=[
            _receipt_material(item)
            for item in sorted(
                captured_materials,
                key=lambda value: value.material_id.encode("utf-8"),
            )
        ],
        input_digest=input_value,
        proposal_digest=proposal_value,
        transaction_digest=transaction_value,
        committed_at=generated_at,
    )
    return ApplicationCommitRequest(
        policy=workspace.profile.config.application_commit_policy,
        transaction_digest=transaction_value,
        base_commit=manifest.base_commit,
        operations=operations,
        receipt=receipt.model_dump(mode="json"),
    )
