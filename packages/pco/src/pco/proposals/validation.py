"""Pure validation of one immutable proposal snapshot."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

from mem_core.delta import validate_structured_delta
from mem_core.errors import MemError

from ..config import WORKSPACE_FORMAT_VERSION
from .markdown import ParsedBusinessObject, parse_business_markdown
from .materials import CapturedMaterial
from .models import InputMaterial, LocalFileMaterial, ProposalArtifactMaterial
from .snapshot import ProposalSnapshot, capture_snapshot_from_root

if TYPE_CHECKING:
    from ..workspace import Workspace


@dataclass(frozen=True, slots=True, init=False)
class RevisionPreview:
    stream: str
    _record_json: bytes

    def __init__(self, stream: str, record: dict[str, Any]) -> None:
        object.__setattr__(self, "stream", stream)
        object.__setattr__(
            self,
            "_record_json",
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8"),
        )

    @property
    def record(self) -> dict[str, Any]:
        """Return a detached record so callers cannot mutate validation state."""

        return json.loads(self._record_json)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    parsed: tuple[ParsedBusinessObject, ...]
    revisions: tuple[RevisionPreview, ...]
    warnings: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "files": len(self.parsed),
            "revisions": [
                {"stream": item.stream, "id": item.record["id"], "revision": item.record["revision"]}
                for item in self.revisions
            ],
            "warnings": list(self.warnings),
        }


def _error(
    code: str,
    message: str,
    *,
    path: str | Path | None = None,
    stream: str | None = None,
    record_id: str | None = None,
    value: Any = None,
) -> MemError:
    return MemError(
        code,
        "proposal_validation",
        message,
        path=None if path is None else str(path),
        stream=stream,
        record_id=record_id,
        value=value,
        recovery=["Correct the proposal and validate the complete review again"],
        canonical_changed=False,
        retry_safe=False,
    )


def _validate_controls(snapshot: ProposalSnapshot, workspace: "Workspace") -> None:
    manifest = snapshot.manifest
    identity = workspace.identity() if callable(workspace.identity) else workspace.identity
    if manifest.base_commit != workspace.repository.head():
        raise _error("PROPOSAL_STALE", "Proposal base commit is not current", value={"expected": workspace.repository.head(), "observed": manifest.base_commit})
    if manifest.workspace_version != identity.get("workspace_format_version") or manifest.workspace_version != WORKSPACE_FORMAT_VERSION:
        raise _error("WORKSPACE_FORMAT_MISMATCH", "Proposal workspace version does not match the opened workspace", value={"proposal": manifest.workspace_version, "workspace": identity.get("workspace_format_version")})
    if manifest.profile_version != workspace.profile.version or manifest.profile_version != identity.get("profile_version"):
        raise _error("WORKSPACE_FORMAT_MISMATCH", "Proposal Profile version does not match the opened workspace", value={"proposal": manifest.profile_version, "profile": workspace.profile.version})


def _preview(parsed: tuple[ParsedBusinessObject, ...], workspace: "Workspace") -> tuple[RevisionPreview, ...]:
    current_by_stream = {
        stream: workspace.repository.current_records(stream)
        for stream in workspace.profile.config.streams
    }
    seen: set[tuple[str, str]] = set()
    previews: list[RevisionPreview] = []
    for item in parsed:
        key = (item.stream, item.record_id)
        if key in seen:
            raise _error("PROPOSAL_REVISION_CONFLICT", "A proposal may target one record ID only once", stream=item.stream, record_id=item.record_id)
        seen.add(key)
        current = current_by_stream[item.stream].get(item.record_id)
        if item.operation == "create":
            if current is not None or item.base_revision is not None:
                raise _error("PROPOSAL_REVISION_CONFLICT", "Create requires no canonical record and no base revision", stream=item.stream, record_id=item.record_id)
        else:
            current_revision = None if current is None else current.get("revision")
            if current is None or item.base_revision != current_revision:
                raise _error(
                    "PROPOSAL_REVISION_CONFLICT",
                    "Revise and retire require the exact current canonical revision",
                    stream=item.stream,
                    record_id=item.record_id,
                    value={"expected": current_revision, "observed": item.base_revision},
                )
        record = item.to_record_preview()
        workspace.profile.validate_record_schema(item.stream, record)
        previews.append(RevisionPreview(item.stream, record))
    return tuple(previews)


def _validate_materials(
    parsed: tuple[ParsedBusinessObject, ...],
    captured: tuple[CapturedMaterial, ...],
    manifest_materials: tuple[InputMaterial, ...],
    *,
    require_hashes: bool,
) -> None:
    by_id = {item.material_id: item for item in captured}
    expected_ids = {item.material_id for item in manifest_materials}
    if set(by_id) != expected_ids:
        raise _error(
            "PROPOSAL_SNAPSHOT_INCOMPLETE",
            "Validation requires every manifest material to be captured",
            value={"missing_material_ids": sorted(expected_ids - set(by_id))},
        )
    for item in captured:
        expected = item.material.expected_content_hash
        observed = item.content_hash
        if expected is None:
            raise _error(
                "INPUT_MATERIAL_HASH_REQUIRED",
                "Input material requires the observed normalized content hash",
                value={"material_id": item.material_id, "observed_content_hash": observed},
            )
        if expected != observed:
            code = (
                "SOURCE_CHANGED_DURING_SNAPSHOT"
                if require_hashes and isinstance(item.material, LocalFileMaterial)
                else "INPUT_MATERIAL_HASH_MISMATCH"
            )
            raise _error(
                code,
                "Input material differs from its reviewed expected hash",
                value={"material_id": item.material_id, "expected_content_hash": expected, "observed_content_hash": observed},
            )
    for item in parsed:
        if item.stream != "sources":
            continue
        if item.material_id is None or item.material_id not in by_id:
            raise _error(
                "PROPOSAL_MATERIAL_MISMATCH",
                "Every proposed source must resolve exactly one captured material_id",
                path=item.relative_path,
                stream=item.stream,
                record_id=item.record_id,
                value=item.material_id,
            )
        material = by_id[item.material_id]
        structured = item.structured
        descriptor = material.material
        source_fidelity = structured.get("fidelity")
        # Manifest fidelity describes how much material was captured, whereas
        # source fidelity describes the evidence claim made from that material.
        # An excerpt snapshot cannot support a full-transcript claim; all other
        # combinations are non-overclaiming (including using only an excerpt
        # from a completely captured source).
        fidelity_matches = not (
            descriptor.fidelity == "selected_excerpt"
            and source_fidelity == "full_user_provided_transcript"
        )
        if not fidelity_matches or (
            isinstance(descriptor, LocalFileMaterial)
            and structured.get("locator") != descriptor.locator
        ):
            raise _error(
                "PROPOSAL_MATERIAL_MISMATCH",
                "Source fields do not match the captured material descriptor",
                path=item.relative_path,
                stream=item.stream,
                record_id=item.record_id,
                value={"material_id": item.material_id},
            )


def validate_snapshot(
    snapshot: ProposalSnapshot,
    workspace: "Workspace",
    *,
    require_hashes: bool,
) -> ValidationResult:
    """Validate only snapshot bytes against the exact canonical base and Profile."""

    _validate_controls(snapshot, workspace)
    manifest_operations = {item.path: item.operation for item in snapshot.manifest.files}
    parsed = tuple(
        parse_business_markdown(item.path, item.data) for item in snapshot.business_files
    )
    for item in parsed:
        if manifest_operations.get(item.relative_path) != item.operation:
            raise _error(
                "PROPOSAL_OPERATION_MISMATCH",
                "Manifest operation does not match Markdown operation",
                path=item.relative_path,
                stream=item.stream,
                record_id=item.record_id,
                value={"manifest": manifest_operations.get(item.relative_path), "markdown": item.operation},
            )
    captured = snapshot.input_material
    _validate_materials(
        parsed,
        captured,
        tuple(snapshot.manifest.input_material),
        require_hashes=require_hashes,
    )
    revisions = _preview(parsed, workspace)
    delta: dict[str, list[dict[str, Any]]] = {
        stream: [] for stream in workspace.profile.config.streams
    }
    for preview in revisions:
        delta[preview.stream].append(preview.record)
    validate_structured_delta(
        profile=workspace.profile,
        base_records=workspace.repository.records_by_stream(),
        delta=delta,
        root=workspace.repository.root,
    )
    return ValidationResult(parsed=parsed, revisions=revisions)


def capture_and_validate_for_commit(
    proposal_root: Path, workspace: "Workspace"
) -> tuple[ProposalSnapshot, ValidationResult]:
    """Narrow Task-4 commit-mode boundary; Task 6 owns public commit orchestration."""

    snapshot = capture_snapshot_from_root(proposal_root, include_local=True)
    return snapshot, validate_snapshot(snapshot, workspace, require_hashes=True)
