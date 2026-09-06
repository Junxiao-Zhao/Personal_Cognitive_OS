from __future__ import annotations

import hashlib
import json
import os
from pathlib import PurePosixPath

from mem_core.errors import MemError
import pytest

from pco.proposals.models import (
    BusinessFile,
    LocalFileMaterial,
    ProposalArtifactMaterial,
    ProposalManifest,
)
from pco.proposals.markdown import ParsedBusinessObject
from pco.proposals.service import ProposalService
from pco.proposals import snapshot as snapshot_module
from pco.proposals.snapshot import capture_snapshot
from pco.proposals.validation import (
    _preview,
    capture_and_validate_for_commit,
    validate_snapshot,
)


def _tree(root):
    return sorted(
        (path.relative_to(root).as_posix(), path.read_bytes() if path.is_file() else None)
        for path in root.rglob("*")
    )


def _make_source_proposal(
    workspace,
    proposal_factory,
    business_markdown_factory,
    local,
    *,
    expected=None,
    material_fidelity="selected_excerpt",
    source_fidelity="registered_source_excerpt",
    user_authored=None,
):
    root = proposal_factory("review_source")
    (root / "sources").mkdir()
    source_fields = {
        "change_summary": "create",
        "material_id": "journal",
        "locator": local.as_uri(),
        "title": "Journal",
        "retrieved_at": "2026-08-30T10:00:00+00:00",
        "provider_label": "local",
        "fidelity": source_fidelity,
        "used_excerpts": ["entry"],
        "status": "active",
    }
    if user_authored is not None:
        source_fields["user_authored"] = user_authored
    source_md = business_markdown_factory(
        object_type="source",
        record_id="src_local",
        fields=source_fields,
    )
    (root / "sources/src_local.md").write_bytes(source_md)
    manifest = ProposalManifest(
        review_id=root.name,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        files=[BusinessFile(path="sources/src_local.md", operation="create")],
        input_material=[
            LocalFileMaterial(
                material_id="journal",
                kind="local_file",
                locator=local.as_uri(),
                expected_content_hash=expected,
                fidelity=material_fidelity,
            )
        ],
    )
    (root / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root, manifest


def test_validate_reports_observed_hash_and_is_read_only(
    workspace, proposal_factory, business_markdown_factory, tmp_path
) -> None:
    local = tmp_path / "journal.md"
    local.write_bytes(b"entry\r\n")
    _make_source_proposal(workspace, proposal_factory, business_markdown_factory, local)
    before = _tree(workspace.root)

    result = ProposalService(workspace).validate("review_source")

    observed = "sha256:" + hashlib.sha256(b"entry\n").hexdigest()
    assert result["ok"] is False
    assert result["error"]["code"] == "INPUT_MATERIAL_HASH_REQUIRED"
    assert result["error"]["value"]["observed_content_hash"] == observed
    assert _tree(workspace.root) == before


def test_ordinary_validation_and_commit_boundary_distinguish_hash_mismatch(
    workspace, proposal_factory, business_markdown_factory, tmp_path
) -> None:
    local = tmp_path / "journal.md"
    local.write_text("changed\n", encoding="utf-8")
    root, manifest = _make_source_proposal(
        workspace,
        proposal_factory,
        business_markdown_factory,
        local,
        expected="sha256:" + "0" * 64,
    )
    snapshot = capture_snapshot(root, manifest, include_local=True)
    with pytest.raises(MemError) as ordinary:
        validate_snapshot(snapshot, workspace, require_hashes=False)
    assert ordinary.value.detail.code == "INPUT_MATERIAL_HASH_MISMATCH"
    with pytest.raises(MemError) as committing:
        capture_and_validate_for_commit(root, workspace)
    assert committing.value.detail.code == "SOURCE_CHANGED_DURING_SNAPSHOT"


def test_validate_rejects_snapshot_that_deliberately_omitted_local_material(
    workspace, proposal_factory, business_markdown_factory, tmp_path
) -> None:
    local = tmp_path / "journal.md"
    local.write_text("entry\n", encoding="utf-8")
    root, manifest = _make_source_proposal(
        workspace,
        proposal_factory,
        business_markdown_factory,
        local,
        expected="sha256:" + hashlib.sha256(b"entry\n").hexdigest(),
    )
    with pytest.raises(MemError) as exc:
        validate_snapshot(
            capture_snapshot(root, manifest, include_local=False),
            workspace,
            require_hashes=False,
        )
    assert exc.value.detail.code == "PROPOSAL_SNAPSHOT_INCOMPLETE"


def test_commit_mode_does_not_translate_artifact_hash_mismatch_to_local_change(
    workspace, proposal_factory
) -> None:
    root = proposal_factory("review_artifact_hash")
    (root / "inputs").mkdir()
    (root / "inputs/excerpt.txt").write_text("excerpt\n", encoding="utf-8")
    manifest = ProposalManifest(
        review_id=root.name,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        input_material=[
            ProposalArtifactMaterial(
                material_id="excerpt",
                kind="proposal_artifact",
                path="inputs/excerpt.txt",
                expected_content_hash="sha256:" + "0" * 64,
                fidelity="selected_excerpt",
            )
        ],
    )
    (root / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json")) + "\n", encoding="utf-8"
    )
    with pytest.raises(MemError) as exc:
        capture_and_validate_for_commit(root, workspace)
    assert exc.value.detail.code == "INPUT_MATERIAL_HASH_MISMATCH"


def test_validation_requires_source_material_binding_and_descriptor_match(
    workspace, proposal_factory, business_markdown_factory, tmp_path
) -> None:
    local = tmp_path / "journal.md"
    local.write_text("entry\n", encoding="utf-8")
    expected = "sha256:" + hashlib.sha256(b"entry\n").hexdigest()
    root, manifest = _make_source_proposal(
        workspace,
        proposal_factory,
        business_markdown_factory,
        local,
        expected=expected,
        source_fidelity="full_user_provided_transcript",
        user_authored=True,
    )
    # The source says selected material fidelity only through its proposal descriptor.
    # Its business fidelity must agree with that descriptor.
    with pytest.raises(MemError) as exc:
        validate_snapshot(
            capture_snapshot(root, manifest, include_local=True),
            workspace,
            require_hashes=False,
        )
    assert exc.value.detail.code == "PROPOSAL_MATERIAL_MISMATCH"


def test_manifest_operation_must_match_markdown(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    root = proposal_factory("review_operation")
    (root / "evidence").mkdir()
    (root / "evidence/ev_one.md").write_bytes(
        business_markdown_factory(
            object_type="evidence",
            record_id="ev_one",
            operation="create",
            fields={
                "change_summary": "create",
                "speaker": "user",
                "source_kind": "conversation",
                "user_authored": True,
                "fidelity": "selected_excerpt",
                "status": "active",
            },
        )
    )
    manifest = ProposalManifest(
        review_id=root.name,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        files=[BusinessFile(path="evidence/ev_one.md", operation="revise")],
    )
    (root / "manifest.json").write_text(json.dumps(manifest.model_dump(mode="json")) + "\n")
    with pytest.raises(MemError) as exc:
        validate_snapshot(capture_snapshot(root, manifest, include_local=True), workspace, require_hashes=False)
    assert exc.value.detail.code == "PROPOSAL_OPERATION_MISMATCH"


def test_validation_rejects_stale_base_before_business_preview(
    workspace, proposal_factory
) -> None:
    root = proposal_factory("review_stale")
    manifest = ProposalManifest(
        review_id=root.name,
        base_commit="0" * 40,
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
    )
    (root / "manifest.json").write_text(json.dumps(manifest.model_dump(mode="json")) + "\n")
    with pytest.raises(MemError) as exc:
        validate_snapshot(capture_snapshot(root, manifest, include_local=True), workspace, require_hashes=False)
    assert exc.value.detail.code == "PROPOSAL_STALE"


def test_complete_delta_reference_validation_is_order_independent(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    root = proposal_factory("review_delta")
    for directory in ("events", "evidence"):
        (root / directory).mkdir()
    event = business_markdown_factory(
        object_type="event", record_id="evt_one",
        fields={"change_summary": "create", "occurred_at": {"precision": "day", "start": "2026-08-30"}, "evidence_refs": ["evidence:ev_one"], "status": "active"},
    )
    evidence = business_markdown_factory(
        object_type="evidence", record_id="ev_one",
        fields={"change_summary": "create", "speaker": "user", "source_kind": "conversation", "user_authored": True, "fidelity": "selected_excerpt", "status": "active"},
    )
    (root / "events/evt_one.md").write_bytes(event)
    (root / "evidence/ev_one.md").write_bytes(evidence)
    manifest = ProposalManifest(
        review_id=root.name,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        files=[BusinessFile(path="events/evt_one.md", operation="create"), BusinessFile(path="evidence/ev_one.md", operation="create")],
    )
    (root / "manifest.json").write_text(json.dumps(manifest.model_dump(mode="json")) + "\n")
    validated = validate_snapshot(capture_snapshot(root, manifest, include_local=True), workspace, require_hashes=False)
    assert [item.record["id"] for item in validated.revisions] == ["evt_one", "ev_one"]


def test_local_full_source_material_binds_registered_source_excerpt(
    workspace, proposal_factory, business_markdown_factory, tmp_path
) -> None:
    local = tmp_path / "journal.md"
    local.write_text("entry\n", encoding="utf-8")
    expected = "sha256:" + hashlib.sha256(b"entry\n").hexdigest()
    root, _manifest_value = _make_source_proposal(
        workspace,
        proposal_factory,
        business_markdown_factory,
        local,
        expected=expected,
        material_fidelity="full_source",
    )

    result = ProposalService(workspace).validate(root.name)

    assert result["ok"] is True


def test_snapshot_local_capture_uses_task2_capability_contract(
    workspace, proposal_factory, business_markdown_factory, tmp_path, monkeypatch
) -> None:
    local = tmp_path / "journal.md"
    local.write_text("entry\n", encoding="utf-8")
    expected = "sha256:" + hashlib.sha256(b"entry\n").hexdigest()
    root, manifest = _make_source_proposal(
        workspace, proposal_factory, business_markdown_factory, local, expected=expected
    )
    def reject_unsafe_platform(*, anchored: bool, path) -> None:
        assert anchored is False
        raise MemError(
            "INPUT_MATERIAL_UNSAFE",
            "proposal_material",
            "injected missing capability",
            path=str(path),
            canonical_changed=False,
            retry_safe=False,
        )

    monkeypatch.setattr(
        snapshot_module, "_require_read_capabilities", reject_unsafe_platform
    )

    with pytest.raises(MemError) as exc:
        capture_snapshot(root, manifest, include_local=True)

    assert exc.value.detail.code == "INPUT_MATERIAL_UNSAFE"


def test_snapshot_opens_external_local_material_once_and_closes_it(
    workspace, proposal_factory, business_markdown_factory, tmp_path, monkeypatch
) -> None:
    local = tmp_path / "journal.md"
    local.write_text("entry\n", encoding="utf-8")
    expected = "sha256:" + hashlib.sha256(b"entry\n").hexdigest()
    root, manifest = _make_source_proposal(
        workspace, proposal_factory, business_markdown_factory, local, expected=expected
    )
    real_open = os.open
    real_close = os.close
    local_descriptors: list[int] = []
    closed: list[int] = []

    def recording_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        if args[0] == local:
            local_descriptors.append(descriptor)
        return descriptor

    def recording_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(snapshot_module.os, "open", recording_open)
    monkeypatch.setattr(snapshot_module.os, "close", recording_close)
    monkeypatch.setattr(
        snapshot_module.os,
        "supports_dir_fd",
        set(os.supports_dir_fd) | {recording_open},
    )

    capture_snapshot(root, manifest, include_local=True)

    assert len(local_descriptors) == 1
    assert local_descriptors[0] in closed


def test_local_material_snapshot_budget_counts_normalized_bytes_exactly(
    workspace,
    proposal_factory,
    business_markdown_factory,
    tmp_path,
    monkeypatch,
) -> None:
    local = tmp_path / "journal.md"
    local.write_bytes(b"a\r\n")
    expected = "sha256:" + hashlib.sha256(b"a\n").hexdigest()
    root, manifest = _make_source_proposal(
        workspace, proposal_factory, business_markdown_factory, local, expected=expected
    )
    exact = len((root / "manifest.json").read_bytes()) + len(
        (root / "sources/src_local.md").read_bytes()
    ) + len(b"a\n")
    monkeypatch.setattr(snapshot_module, "MAX_IMMUTABLE_SNAPSHOT_BYTES", exact)

    assert capture_snapshot(root, manifest, include_local=True).total_bytes == exact
    monkeypatch.setattr(snapshot_module, "MAX_IMMUTABLE_SNAPSHOT_BYTES", exact - 1)
    with pytest.raises(MemError) as exc:
        capture_snapshot(root, manifest, include_local=True)
    assert exc.value.detail.code == "PROPOSAL_SNAPSHOT_TOO_LARGE"


def test_proposal_artifact_binds_a_source_without_mutable_external_input(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    root = proposal_factory("review_artifact_source")
    (root / "sources").mkdir()
    (root / "inputs").mkdir()
    material_bytes = b"reviewed excerpt\n"
    expected = "sha256:" + hashlib.sha256(material_bytes).hexdigest()
    (root / "inputs/excerpt.txt").write_bytes(material_bytes)
    (root / "sources/src_remote.md").write_bytes(
        business_markdown_factory(
            object_type="source",
            record_id="src_remote",
            fields={
                "change_summary": "create",
                "material_id": "excerpt",
                "locator": "https://example.invalid/source",
                "title": "Remote source",
                "retrieved_at": "2026-08-30T10:00:00+00:00",
                "provider_label": "browser",
                "fidelity": "selected_excerpt",
                "used_excerpts": ["reviewed excerpt"],
                "status": "active",
            },
        )
    )
    manifest = ProposalManifest(
        review_id=root.name,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        files=[BusinessFile(path="sources/src_remote.md", operation="create")],
        input_material=[
            ProposalArtifactMaterial(
                material_id="excerpt",
                kind="proposal_artifact",
                path="inputs/excerpt.txt",
                expected_content_hash=expected,
                fidelity="selected_excerpt",
            )
        ],
    )
    (root / "manifest.json").write_text(json.dumps(manifest.model_dump(mode="json")) + "\n")

    result = validate_snapshot(
        capture_snapshot(root, manifest, include_local=True),
        workspace,
        require_hashes=False,
    )

    assert result.as_dict()["ok"] is True


def test_validation_revision_records_are_detached_from_the_retained_result(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    root = proposal_factory("review_detached_result")
    (root / "evidence").mkdir()
    (root / "evidence/ev_one.md").write_bytes(
        business_markdown_factory(
            object_type="evidence",
            record_id="ev_one",
            fields={
                "change_summary": "create",
                "speaker": "user",
                "source_kind": "conversation",
                "user_authored": True,
                "fidelity": "selected_excerpt",
                "status": "active",
            },
        )
    )
    manifest = ProposalManifest(
        review_id=root.name,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        files=[BusinessFile(path="evidence/ev_one.md", operation="create")],
    )
    (root / "manifest.json").write_text(json.dumps(manifest.model_dump(mode="json")) + "\n")
    result = validate_snapshot(
        capture_snapshot(root, manifest, include_local=True),
        workspace,
        require_hashes=False,
    )

    exposed = result.revisions[0].record
    exposed["payload"]["speaker"] = "assistant"

    assert result.revisions[0].record["payload"]["speaker"] == "user"


def test_external_evidence_cannot_support_a_personal_event_even_in_same_delta(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    root = proposal_factory("review_external_personal_fact")
    for directory in ("events", "evidence"):
        (root / directory).mkdir()
    (root / "evidence/ev_external.md").write_bytes(
        business_markdown_factory(
            object_type="evidence",
            record_id="ev_external",
            fields={
                "change_summary": "create",
                "speaker": "researcher",
                "source_kind": "external",
                "user_authored": False,
                "fidelity": "selected_excerpt",
                "status": "active",
            },
        )
    )
    (root / "events/evt_personal.md").write_bytes(
        business_markdown_factory(
            object_type="event",
            record_id="evt_personal",
            fields={
                "change_summary": "create",
                "occurred_at": {"precision": "day", "start": "2026-08-30"},
                "evidence_refs": ["evidence:ev_external"],
                "status": "active",
            },
        )
    )
    manifest = ProposalManifest(
        review_id=root.name,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        files=[
            BusinessFile(path="events/evt_personal.md", operation="create"),
            BusinessFile(path="evidence/ev_external.md", operation="create"),
        ],
    )
    (root / "manifest.json").write_text(json.dumps(manifest.model_dump(mode="json")) + "\n")

    with pytest.raises(MemError) as exc:
        validate_snapshot(
            capture_snapshot(root, manifest, include_local=True),
            workspace,
            require_hashes=False,
        )

    assert exc.value.detail.code == "EVIDENCE_INELIGIBLE"


def test_service_validate_rejects_a_different_requested_review_without_reads(
    workspace, proposal_factory
) -> None:
    root = proposal_factory("review_actual")
    manifest = ProposalManifest(
        review_id=root.name,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
    )
    (root / "manifest.json").write_text(json.dumps(manifest.model_dump(mode="json")) + "\n")

    result = ProposalService(workspace).validate("review_other")

    assert result["ok"] is False
    assert result["error"]["code"] == "PROPOSAL_MANIFEST_MISMATCH"


@pytest.mark.parametrize("control", ["workspace_version", "profile_version"])
def test_validation_rejects_workspace_and_profile_control_mismatches(
    workspace, proposal_factory, control
) -> None:
    root = proposal_factory(f"review_bad_{control}")
    values = {
        "review_id": root.name,
        "base_commit": workspace.repository.head(),
        "workspace_version": workspace.identity["workspace_format_version"],
        "profile_version": workspace.profile.version,
    }
    values[control] = "0.5.999"
    manifest = ProposalManifest(**values)
    (root / "manifest.json").write_text(json.dumps(manifest.model_dump(mode="json")) + "\n")

    with pytest.raises(MemError) as exc:
        validate_snapshot(
            capture_snapshot(root, manifest, include_local=True),
            workspace,
            require_hashes=False,
        )

    assert exc.value.detail.code == "WORKSPACE_FORMAT_MISMATCH"


@pytest.mark.parametrize(
    ("operation", "base_revision", "has_current", "expected_ok"),
    [
        ("create", None, True, False),
        ("revise", 1, False, False),
        ("revise", 2, True, False),
        ("revise", 1, True, True),
        ("retire", 1, True, True),
    ],
)
def test_revision_preview_covers_create_revise_retire_conflicts(
    workspace,
    proposal_factory,
    business_markdown_factory,
    monkeypatch,
    operation,
    base_revision,
    has_current,
    expected_ok,
) -> None:
    root = proposal_factory(f"review_revision_{operation}_{base_revision}_{has_current}")
    (root / "evidence").mkdir()
    fields = {
        "change_summary": operation,
        "speaker": "user",
        "source_kind": "conversation",
        "user_authored": True,
        "fidelity": "selected_excerpt",
        "status": "tombstone" if operation == "retire" else "active",
    }
    (root / "evidence/ev_existing.md").write_bytes(
        business_markdown_factory(
            object_type="evidence",
            record_id="ev_existing",
            operation=operation,
            base_revision=base_revision,
            fields=fields,
        )
    )
    manifest = ProposalManifest(
        review_id=root.name,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        files=[BusinessFile(path="evidence/ev_existing.md", operation=operation)],
    )
    (root / "manifest.json").write_text(json.dumps(manifest.model_dump(mode="json")) + "\n")
    current = {
        "id": "ev_existing",
        "revision": 1,
        "recorded_at": "2026-08-30T10:00:00+00:00",
        "schema_version": "pco/evidence/v1",
        "commit_ref": "commit_existing",
        "payload": {
            "previous_revision": None,
            "change_summary": "create",
            "body_markdown": "existing\n",
            "speaker": "user",
            "source_kind": "conversation",
            "user_authored": True,
            "fidelity": "selected_excerpt",
            "status": "active",
        },
    }
    def current_records(stream):
        if stream == "evidence" and has_current:
            return {"ev_existing": current}
        return {}

    def records_by_stream():
        records = {stream: [] for stream in workspace.profile.config.streams}
        if has_current:
            records["evidence"] = [current]
        return records

    monkeypatch.setattr(workspace.repository, "current_records", current_records)
    monkeypatch.setattr(workspace.repository, "records_by_stream", records_by_stream)
    snapshot = capture_snapshot(root, manifest, include_local=True)

    if not expected_ok:
        with pytest.raises(MemError) as exc:
            validate_snapshot(snapshot, workspace, require_hashes=False)
        assert exc.value.detail.code == "PROPOSAL_REVISION_CONFLICT"
    else:
        result = validate_snapshot(snapshot, workspace, require_hashes=False)
        assert result.revisions[0].record["revision"] == 2


def test_revision_preview_rejects_two_targets_for_one_stream_record(workspace) -> None:
    semantics = {
        "change_summary": "create",
        "speaker": "user",
        "source_kind": "conversation",
        "user_authored": True,
        "fidelity": "selected_excerpt",
        "status": "active",
    }
    parsed = tuple(
        ParsedBusinessObject(
            stream="evidence",
            operation="create",
            record_id="ev_duplicate",
            base_revision=None,
            structured=semantics,
            body_markdown="reviewed\n",
            relative_path=PurePosixPath(path),
        )
        for path in ("evidence/first.md", "evidence/second.md")
    )

    with pytest.raises(MemError) as exc:
        _preview(parsed, workspace)

    assert exc.value.detail.code == "PROPOSAL_REVISION_CONFLICT"
