from __future__ import annotations

import hashlib
import json

from mem_core.application import application_transaction_id
from mem_core.errors import MemError
from mem_core.models import ApplicationCommitRequest
import pytest
from pydantic import ValidationError

from pco.proposals.models import (
    BusinessFile,
    ProposalArtifactMaterial,
    ProposalCommitReceipt,
    ProposalManifest,
)
from pco.proposals.operations import (
    SemanticNoOp,
    build_commit_request,
    replay_warnings,
)
from pco.proposals.snapshot import capture_snapshot_from_root
from pco.proposals.validation import validate_snapshot


def _source_proposal(
    workspace,
    proposal_factory,
    business_markdown_factory,
    *,
    review_id="review_build",
    content=b"captured excerpt\n",
    title="Reviewed source",
):
    root = proposal_factory(review_id)
    (root / "inputs").mkdir()
    (root / "sources").mkdir()
    (root / "inputs/excerpt.txt").write_bytes(content)
    content_hash = "sha256:" + hashlib.sha256(content).hexdigest()
    (root / "sources/src_build.md").write_bytes(
        business_markdown_factory(
            object_type="source",
            record_id="src_build",
            fields={
                "change_summary": "Create source",
                "material_id": "excerpt",
                "locator": "https://example.invalid/source",
                "title": title,
                "retrieved_at": "2026-08-30T10:00:00+00:00",
                "provider_label": "browser",
                "fidelity": "selected_excerpt",
                "used_excerpts": ["captured excerpt"],
                "status": "active",
            },
            body="Source body.\n",
        )
    )
    manifest = ProposalManifest(
        review_id=review_id,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        input_material=[
            ProposalArtifactMaterial(
                material_id="excerpt",
                kind="proposal_artifact",
                path="inputs/excerpt.txt",
                expected_content_hash=content_hash,
                fidelity="selected_excerpt",
            )
        ],
        files=[BusinessFile(path="sources/src_build.md", operation="create")],
    )
    (root / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    snapshot = capture_snapshot_from_root(root, include_local=True)
    return root, snapshot, validate_snapshot(snapshot, workspace, require_hashes=True)


def test_build_commit_request_generates_source_blob_revision_and_strict_receipt(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    _root, snapshot, validated = _source_proposal(
        workspace, proposal_factory, business_markdown_factory
    )
    monkeypatch.setattr(
        "pco.proposals.operations.utc_now",
        lambda: "2026-09-01T12:34:56+00:00",
    )

    request = build_commit_request(workspace, snapshot, validated)

    assert isinstance(request, ApplicationCommitRequest)
    assert "transaction_id" not in ApplicationCommitRequest.model_fields
    assert application_transaction_id(request.transaction_digest).startswith("txn_")
    assert request.policy == "pco_proposal"
    assert [operation.op for operation in request.operations] == ["write_artifact", "append"]
    blob, append = request.operations
    expected_hash = "sha256:" + hashlib.sha256(b"captured excerpt\n").hexdigest()
    assert blob.path == "source_blobs/" + expected_hash.removeprefix("sha256:")
    assert blob.content == "captured excerpt\n"
    assert append.stream == "sources"
    assert append.record["recorded_at"] == "2026-09-01T12:34:56+00:00"
    assert append.record["commit_ref"] == request.receipt["receipt_id"]
    assert append.record["payload"]["content_hash"] == expected_hash
    assert append.record["payload"]["snapshot_ref"] == blob.path
    receipt = ProposalCommitReceipt.model_validate(request.receipt)
    assert receipt.committed_at == append.record["recorded_at"]
    assert receipt.files[0].path.as_posix() == "sources/src_build.md"
    assert receipt.input_material[0].file_hash is not None
    assert receipt.transaction_digest == request.transaction_digest


def test_build_request_is_retry_stable_except_for_excluded_timestamps(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    _root, snapshot, validated = _source_proposal(
        workspace, proposal_factory, business_markdown_factory
    )
    times = iter(("2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00"))
    monkeypatch.setattr("pco.proposals.operations.utc_now", lambda: next(times))
    first = build_commit_request(workspace, snapshot, validated)
    second = build_commit_request(workspace, snapshot, validated)
    assert isinstance(first, ApplicationCommitRequest)
    assert isinstance(second, ApplicationCommitRequest)
    assert first.transaction_digest == second.transaction_digest
    assert first.receipt["receipt_id"] == second.receipt["receipt_id"]
    assert first.operations[1].record["recorded_at"] != second.operations[1].record["recorded_at"]
    assert first.receipt["committed_at"] != second.receipt["committed_at"]


def test_existing_blob_is_read_from_base_and_deduplicated(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    content = b"captured excerpt\n"
    hex_digest = hashlib.sha256(content).hexdigest()
    blob = workspace.repository.root / "source_blobs" / hex_digest
    blob.write_bytes(content)
    workspace.repository._git("add", ".")
    workspace.repository._git("commit", "--no-verify", "-m", "seed valid blob")
    _root, snapshot, validated = _source_proposal(
        workspace, proposal_factory, business_markdown_factory, content=content
    )

    request = build_commit_request(workspace, snapshot, validated)
    assert isinstance(request, ApplicationCommitRequest)
    assert [operation.op for operation in request.operations] == ["append"]


def test_hash_invalid_blob_at_content_addressed_path_is_rejected(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    content = b"captured excerpt\n"
    hex_digest = hashlib.sha256(content).hexdigest()
    blob = workspace.repository.root / "source_blobs" / hex_digest
    blob.write_bytes(b"corrupt\n")
    workspace.repository._git("add", ".")
    workspace.repository._git("commit", "--no-verify", "-m", "seed corrupt blob")
    _root, snapshot, validated = _source_proposal(
        workspace, proposal_factory, business_markdown_factory, content=content
    )

    with pytest.raises(MemError) as exc:
        build_commit_request(workspace, snapshot, validated)
    assert exc.value.detail.code == "SOURCE_SNAPSHOT_INVALID"


def test_local_file_receipt_has_no_proposal_file_hash() -> None:
    with pytest.raises(ValidationError):
        ProposalCommitReceipt.model_validate(
            {
                "type": "proposal_commit_receipt",
                "receipt_id": "commit_" + "a" * 64,
                "workspace_id": "workspace_a",
                "review_id": "review_a",
                "manifest_hash": "sha256:" + "b" * 64,
                "files": [],
                "input_material": [
                    {
                        "material_id": "local",
                        "kind": "local_file",
                        "locator": "file:///tmp/a",
                        "hash": "sha256:" + "c" * 64,
                        "file_hash": "sha256:" + "d" * 64,
                    }
                ],
                "input_digest": "sha256:" + "e" * 64,
                "proposal_digest": "sha256:" + "f" * 64,
                "transaction_digest": "sha256:" + "1" * 64,
                "committed_at": "2026-09-01T00:00:00+00:00",
            }
        )


def test_semantic_noop_requires_both_prior_digests_and_current_record_equivalence(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    _root, snapshot, validated = _source_proposal(
        workspace, proposal_factory, business_markdown_factory
    )
    request = build_commit_request(workspace, snapshot, validated)
    assert isinstance(request, ApplicationCommitRequest)
    # Simulate the exact state a concurrent equivalent application produced,
    # while keeping this captured/validated proposal immutable.
    for operation in request.operations:
        if operation.op == "write_artifact":
            target = workspace.repository.root / operation.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(operation.content, encoding="utf-8")
        else:
            operation.record["commit_ref"] = "commit_" + "9" * 64
            workspace.repository.append_record(
                workspace.repository.root, operation.stream, operation.record
            )
    transaction = {
        "id": application_transaction_id(request.transaction_digest),
        "application_policy": request.policy,
        "base_commit": request.base_commit,
        "operation_count": len(request.operations),
        "operations": [operation.normalized() for operation in request.operations],
        "transaction_fingerprint": request.transaction_digest,
        "receipt": request.receipt,
    }
    with (workspace.repository.root / "transactions/transactions.jsonl").open(
        "a", encoding="utf-8"
    ) as handle:
        handle.write(json.dumps(transaction, sort_keys=True) + "\n")
    workspace.repository._git("add", ".")
    workspace.repository._git("commit", "--no-verify", "-m", "seed equivalent proposal")
    # The builder is deliberately anchored by the immutable snapshot base. For
    # this no-op branch, point the manifest at the state containing the prior
    # receipt without changing the already validated semantic previews.
    changed = json.loads(snapshot.manifest_bytes)
    changed["base_commit"] = workspace.repository.head()
    object.__setattr__(
        snapshot,
        "manifest_bytes",
        (json.dumps(changed, sort_keys=True) + "\n").encode("utf-8"),
    )

    result = build_commit_request(workspace, snapshot, validated)
    assert isinstance(result, SemanticNoOp)
    assert result.equivalent_records[0]["id"] == "src_build"


def test_same_input_and_prior_receipt_with_changed_proposal_is_not_noop(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    _root, snapshot, validated = _source_proposal(
        workspace, proposal_factory, business_markdown_factory
    )
    request = build_commit_request(workspace, snapshot, validated)
    assert isinstance(request, ApplicationCommitRequest)
    prior = {
        "id": application_transaction_id(request.transaction_digest),
        "application_policy": request.policy,
        "base_commit": request.base_commit,
        "operation_count": len(request.operations),
        "operations": [operation.normalized() for operation in request.operations],
        "transaction_fingerprint": request.transaction_digest,
        "receipt": {**request.receipt, "proposal_digest": "sha256:" + "0" * 64},
    }
    with (workspace.repository.root / "transactions/transactions.jsonl").open(
        "a", encoding="utf-8"
    ) as handle:
        handle.write(json.dumps(prior, sort_keys=True) + "\n")
    workspace.repository._git("add", ".")
    workspace.repository._git("commit", "--no-verify", "-m", "seed input replay only")
    changed = json.loads(snapshot.manifest_bytes)
    changed["base_commit"] = workspace.repository.head()
    object.__setattr__(snapshot, "manifest_bytes", (json.dumps(changed) + "\n").encode())

    revalidated = validate_snapshot(snapshot, workspace, require_hashes=True)
    assert revalidated.warnings[0]["code"] == "INPUT_REPLAY"
    assert isinstance(
        build_commit_request(workspace, snapshot, revalidated),
        ApplicationCommitRequest,
    )
    assert replay_warnings(workspace, snapshot)[0]["code"] == "INPUT_REPLAY"


def test_build_rejects_validation_result_from_different_snapshot(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    _root, snapshot, _validated = _source_proposal(
        workspace,
        proposal_factory,
        business_markdown_factory,
        review_id="review_original",
        content=b"original\n",
    )
    _other_root, _other_snapshot, other_validated = _source_proposal(
        workspace,
        proposal_factory,
        business_markdown_factory,
        review_id="review_other",
        content=b"other\n",
        title="Different reviewed source",
    )

    with pytest.raises(MemError) as exc:
        build_commit_request(workspace, snapshot, other_validated)
    assert exc.value.detail.code == "PROPOSAL_SNAPSHOT_INCOMPLETE"


def test_prior_matching_digests_without_current_equivalence_is_not_noop(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    _root, snapshot, validated = _source_proposal(
        workspace, proposal_factory, business_markdown_factory
    )
    request = build_commit_request(workspace, snapshot, validated)
    assert isinstance(request, ApplicationCommitRequest)
    transaction = {
        "id": application_transaction_id(request.transaction_digest),
        "application_policy": request.policy,
        "base_commit": request.base_commit,
        "operation_count": len(request.operations),
        "operations": [operation.normalized() for operation in request.operations],
        "transaction_fingerprint": request.transaction_digest,
        "receipt": request.receipt,
    }
    with (workspace.repository.root / "transactions/transactions.jsonl").open(
        "a", encoding="utf-8"
    ) as handle:
        handle.write(json.dumps(transaction, sort_keys=True) + "\n")
    workspace.repository._git("add", ".")
    workspace.repository._git(
        "commit", "--no-verify", "-m", "seed receipt without business state"
    )
    changed = json.loads(snapshot.manifest_bytes)
    changed["base_commit"] = workspace.repository.head()
    object.__setattr__(snapshot, "manifest_bytes", (json.dumps(changed) + "\n").encode())

    assert isinstance(
        build_commit_request(workspace, snapshot, validated), ApplicationCommitRequest
    )
