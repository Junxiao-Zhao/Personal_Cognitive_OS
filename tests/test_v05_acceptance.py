from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from pco.proposals.models import (
    BusinessFile,
    ProposalArtifactMaterial,
    ProposalManifest,
)
from pco.proposals.service import ProposalService
from pco.proposals import service as service_module
from pco.verify import verify_projection

APPROVED_BUSINESS_STREAMS = {
    "evidence",
    "sources",
    "events",
    "psychologies",
    "philosophies",
    "archetypes",
    "hypotheses",
    "meta_revisions",
    "session_handoffs",
}

PROPOSAL_PATHS = {
    "evidence": "evidence/ev_1.md",
    "sources": "sources/src_1.md",
    "events": "events/evt_1.md",
    "psychologies": "psychologies/psy_1.md",
    "philosophies": "philosophies/phi_1.md",
    "archetypes": "archetypes/arc_1.md",
    "hypotheses": "hypotheses/hyp_1.md",
    "meta_revisions": "META_MEMORY.md",
    "session_handoffs": "SESSION_HANDOFF.md",
}

OBJECT_TYPES = {
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


def _write_manifest(root: Path, manifest: ProposalManifest) -> None:
    (root / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _author_cold_start_proposal(
    workspace, minimal_records, business_markdown_factory, *, review_id: str
) -> ProposalManifest:
    root = workspace.paths.proposals / review_id
    root.mkdir()
    source_material = b"Complete reviewed acceptance source.\n"
    (root / "inputs").mkdir()
    (root / "inputs/source.txt").write_bytes(source_material)

    files: list[BusinessFile] = []
    for stream, relative in PROPOSAL_PATHS.items():
        record = deepcopy(minimal_records[stream])
        fields = dict(record["payload"])
        body = fields.pop("body_markdown")
        fields.pop("previous_revision")
        if stream == "sources":
            fields.pop("content_hash")
            fields.pop("snapshot_ref")
            fields["material_id"] = "acceptance_source"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            business_markdown_factory(
                object_type=OBJECT_TYPES[stream],
                record_id=record["id"],
                fields=fields,
                body=body,
            )
        )
        files.append(BusinessFile(path=relative, operation="create"))

    manifest = ProposalManifest(
        review_id=review_id,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        files=files,
        input_material=[
            ProposalArtifactMaterial(
                material_id="acceptance_source",
                kind="proposal_artifact",
                path="inputs/source.txt",
                expected_content_hash="sha256:"
                + hashlib.sha256(source_material).hexdigest(),
                fidelity="full_source",
            )
        ],
    )
    _write_manifest(root, manifest)
    return manifest


def _transaction_count(workspace, receipt_id: str) -> int:
    ledger = workspace.repository.root / "transactions/transactions.jsonl"
    return sum(
        json.loads(line).get("receipt", {}).get("receipt_id") == receipt_id
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def test_cold_start_commits_all_cognitive_classes_and_meta(
    workspace, minimal_records, business_markdown_factory
) -> None:
    proposal = _author_cold_start_proposal(
        workspace,
        minimal_records,
        business_markdown_factory,
        review_id="review_acceptance_cold_start",
    )

    result = ProposalService(workspace).commit(proposal.review_id)

    assert result["canonical_status"] == "committed"
    assert result["derivation"]["ok"] is True
    assert {
        stream
        for stream in workspace.profile.config.streams
        if workspace.repository.record_history(stream, minimal_records[stream]["id"])
    } == APPROVED_BUSINESS_STREAMS
    assert (
        verify_projection(
            workspace.paths.projection,
            expected_commit=workspace.repository.head(),
        )["ok"]
        is True
    )


def test_correction_preserves_history_and_updates_current(
    workspace_with_hypothesis, business_markdown_factory
) -> None:
    workspace = workspace_with_hypothesis
    review_id = "review_acceptance_revision"
    body = "The corrected acceptance hypothesis."
    root = workspace.paths.proposals / review_id
    root.mkdir()
    relative = "hypotheses/hyp_1.md"
    (root / "hypotheses").mkdir()
    (root / relative).write_bytes(
        business_markdown_factory(
            object_type="hypothesis",
            record_id="hyp_1",
            operation="revise",
            base_revision=1,
            fields={
                "change_summary": "Correct the acceptance hypothesis",
                "confidence": 0.8,
                "evidence_refs": ["evidence:ev_cli"],
                "counter_evidence_refs": [],
                "status": "hypothesis",
            },
            body=body,
        )
    )
    proposal = ProposalManifest(
        review_id=review_id,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        files=[BusinessFile(path=relative, operation="revise")],
    )
    _write_manifest(root, proposal)

    result = ProposalService(workspace).commit(proposal.review_id)
    history = workspace.repository.record_history("hypotheses", "hyp_1")

    assert result["canonical_status"] == "committed"
    assert [item["revision"] for item in history] == [1, 2]
    assert (
        workspace.repository.current_records("hypotheses")["hyp_1"]["payload"][
            "body_markdown"
        ]
        == f"\n{body}\n"
    )
    assert (
        verify_projection(
            workspace.paths.projection,
            expected_commit=workspace.repository.head(),
        )["ok"]
        is True
    )


def test_denied_commit_is_modeled_by_not_invoking_command(
    workspace, minimal_records, business_markdown_factory
) -> None:
    proposal = _author_cold_start_proposal(
        workspace,
        minimal_records,
        business_markdown_factory,
        review_id="review_acceptance_denied",
    )
    before = workspace.repository.head()

    # Host denial means the commit command is never invoked.
    assert proposal.review_id == "review_acceptance_denied"
    assert (workspace.paths.proposals / proposal.review_id / "manifest.json").is_file()
    assert workspace.repository.head() == before
    assert all(
        not workspace.repository.record_history(stream, minimal_records[stream]["id"])
        for stream in APPROVED_BUSINESS_STREAMS
    )


def test_canonical_commit_survives_derivation_failure_and_retry_is_idempotent(
    workspace,
    minimal_records,
    business_markdown_factory,
    monkeypatch,
) -> None:
    proposal = _author_cold_start_proposal(
        workspace,
        minimal_records,
        business_markdown_factory,
        review_id="review_acceptance_derivation_failure",
    )
    monkeypatch.setattr(
        service_module,
        "derive_after_commit",
        lambda _workspace: {"ok": False, "error": "injected"},
    )

    first = ProposalService(workspace).commit(proposal.review_id)
    committed_head = workspace.repository.head()
    retry = ProposalService(workspace).commit(proposal.review_id)

    assert first["canonical_status"] == retry["canonical_status"] == "committed"
    assert first["receipt_id"] == retry["receipt_id"]
    assert (
        first["derivation"]
        == retry["derivation"]
        == {
            "ok": False,
            "error": "injected",
        }
    )
    assert workspace.repository.head() == committed_head
    assert _transaction_count(workspace, first["receipt_id"]) == 1
    assert (
        workspace.repository.record_history("hypotheses", "hyp_1")[0]["commit_ref"]
        == first["receipt_id"]
    )
