from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy

from mem_core.application import application_transaction_id
from mem_core.errors import MemError
from mem_core.models import ApplicationCommitRequest
from mem_core.transaction import TransactionManager
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
    record_id="src_build",
):
    root = proposal_factory(review_id)
    (root / "inputs").mkdir()
    (root / "sources").mkdir()
    (root / "inputs/excerpt.txt").write_bytes(content)
    content_hash = "sha256:" + hashlib.sha256(content).hexdigest()
    business_path = f"sources/{record_id}.md"
    (root / business_path).write_bytes(
        business_markdown_factory(
            object_type="source",
            record_id=record_id,
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
        files=[BusinessFile(path=business_path, operation="create")],
    )
    (root / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    snapshot = capture_snapshot_from_root(root, include_local=True)
    return root, snapshot, validate_snapshot(snapshot, workspace, require_hashes=True)


def _committed_source_request(
    workspace, proposal_factory, business_markdown_factory
) -> tuple[object, ApplicationCommitRequest]:
    root, snapshot, validated = _source_proposal(
        workspace,
        proposal_factory,
        business_markdown_factory,
        review_id="review_ledger_seed",
    )
    request = build_commit_request(workspace, snapshot, validated)
    assert isinstance(request, ApplicationCommitRequest)
    TransactionManager(workspace.repository, workspace.paths.tmp).commit_application(
        request
    )
    return root, request


def _snapshot_at_current_head(workspace, root):
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["base_commit"] = workspace.repository.head()
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    return capture_snapshot_from_root(root, include_local=True)


def _commit_corrupt_ledger(workspace, raw: str) -> None:
    ledger = workspace.repository.root / "transactions/transactions.jsonl"
    ledger.write_text(raw, encoding="utf-8")
    workspace.repository._git("add", "transactions/transactions.jsonl")
    workspace.repository._git(
        "commit", "--no-verify", "-m", "inject malformed proposal ledger"
    )


def _unicode_evidence_proposal(
    workspace, proposal_factory, business_markdown_factory
):
    record_id = "用户:偏好"
    root = proposal_factory("review_unicode")
    (root / "evidence").mkdir()
    business_path = f"evidence/{record_id}.md"
    (root / business_path).write_bytes(
        business_markdown_factory(
            object_type="evidence",
            record_id=record_id,
            fields={
                "change_summary": "记录用户偏好",
                "speaker": "user",
                "source_kind": "conversation",
                "user_authored": True,
                "fidelity": "selected_excerpt",
                "status": "active",
            },
            body="用户明确表达的偏好。\n",
        )
    )
    manifest = ProposalManifest(
        review_id=root.name,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        files=[BusinessFile(path=business_path, operation="create")],
    )
    (root / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    snapshot = capture_snapshot_from_root(root, include_local=True)
    return snapshot, validate_snapshot(snapshot, workspace, require_hashes=True)


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


def test_unicode_and_colon_id_flows_through_real_application_commit(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    snapshot, validated = _unicode_evidence_proposal(
        workspace, proposal_factory, business_markdown_factory
    )
    request = build_commit_request(workspace, snapshot, validated)
    assert isinstance(request, ApplicationCommitRequest)

    TransactionManager(workspace.repository, workspace.paths.tmp).commit_application(
        request
    )

    current = workspace.repository.current_records("evidence")
    assert current["用户:偏好"]["id"] == "用户:偏好"


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
    workspace, proposal_factory, business_markdown_factory
) -> None:
    prior_root, prior_snapshot, prior_validated = _source_proposal(
        workspace,
        proposal_factory,
        business_markdown_factory,
        review_id="review_prior",
    )
    prior_request = build_commit_request(workspace, prior_snapshot, prior_validated)
    assert isinstance(prior_request, ApplicationCommitRequest)
    TransactionManager(workspace.repository, workspace.paths.tmp).commit_application(
        prior_request
    )
    shutil.rmtree(prior_root)

    _root, snapshot, validated = _source_proposal(
        workspace,
        proposal_factory,
        business_markdown_factory,
        review_id="review_again",
    )

    result = build_commit_request(workspace, snapshot, validated)
    assert isinstance(result, SemanticNoOp)
    assert result.equivalent_records[0]["id"] == "src_build"
    assert result.warnings[0]["code"] == "INPUT_REPLAY"


def test_same_input_and_prior_receipt_with_changed_proposal_is_not_noop(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    prior_root, prior_snapshot, prior_validated = _source_proposal(
        workspace,
        proposal_factory,
        business_markdown_factory,
        review_id="review_replay_prior",
    )
    prior_request = build_commit_request(
        workspace, prior_snapshot, prior_validated
    )
    assert isinstance(prior_request, ApplicationCommitRequest)
    TransactionManager(workspace.repository, workspace.paths.tmp).commit_application(
        prior_request
    )
    shutil.rmtree(prior_root)

    _root, snapshot, revalidated = _source_proposal(
        workspace,
        proposal_factory,
        business_markdown_factory,
        review_id="review_replay_new_output",
        record_id="src_new_output",
        title="New business output",
    )

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


def test_build_rejects_old_validation_when_only_material_bytes_changed(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    root, _snapshot, validated = _source_proposal(
        workspace, proposal_factory, business_markdown_factory
    )
    (root / "inputs/excerpt.txt").write_bytes(b"changed after validation\n")
    changed_snapshot = capture_snapshot_from_root(root, include_local=True)

    with pytest.raises(MemError) as exc:
        build_commit_request(workspace, changed_snapshot, validated)
    assert exc.value.detail.code == "PROPOSAL_SNAPSHOT_INCOMPLETE"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda manifest: manifest["files"][0].update(operation="revise"),
            id="operation",
        ),
        pytest.param(
            lambda manifest: manifest["input_material"][0].update(
                expected_content_hash="sha256:" + "0" * 64
            ),
            id="expected-content-hash",
        ),
        pytest.param(
            lambda manifest: manifest.update(base_commit="0" * 40),
            id="base-commit",
        ),
    ],
)
def test_build_rejects_old_validation_when_only_one_manifest_control_changed(
    workspace, proposal_factory, business_markdown_factory, mutate
) -> None:
    root, _snapshot, validated = _source_proposal(
        workspace, proposal_factory, business_markdown_factory
    )
    manifest = json.loads((root / "manifest.json").read_bytes())
    mutate(manifest)
    (root / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    changed_snapshot = capture_snapshot_from_root(root, include_local=True)

    with pytest.raises(MemError) as exc:
        build_commit_request(workspace, changed_snapshot, validated)
    assert exc.value.detail.code == "PROPOSAL_SNAPSHOT_INCOMPLETE"


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            lambda line: {**line, "id": "txn_" + "0" * 64},
            id="derived-transaction-id",
        ),
        pytest.param(
            lambda line: {**line, "operation_count": line["operation_count"] + 1},
            id="operation-count",
        ),
        pytest.param(
            lambda line: {
                **line,
                "operations": [{"op": "write_artifact", "path": "source_blobs/bad"}],
            },
            id="operation-shape",
        ),
        pytest.param(
            lambda line: {
                **line,
                "receipt": {**line["receipt"], "workspace_id": "workspace_other"},
            },
            id="workspace-ownership",
        ),
        pytest.param(
            lambda line: {
                **line,
                "operations": [
                    *line["operations"][:-1],
                    {
                        **line["operations"][-1],
                        "record": {
                            **line["operations"][-1]["record"],
                            "payload": {
                                **line["operations"][-1]["record"]["payload"],
                                "title": "Fingerprint bypass",
                            },
                        },
                    },
                ],
            },
            id="digest-bound-operation",
        ),
        pytest.param(
            lambda line: {**line, "operations": list(reversed(line["operations"]))},
            id="operation-order",
        ),
    ],
)
def test_replay_rejects_malformed_proposal_transaction_fields(
    workspace, proposal_factory, business_markdown_factory, mutation
) -> None:
    root, _request = _committed_source_request(
        workspace, proposal_factory, business_markdown_factory
    )
    ledger = workspace.repository.root / "transactions/transactions.jsonl"
    line = json.loads(ledger.read_text(encoding="utf-8"))
    _commit_corrupt_ledger(
        workspace,
        json.dumps(mutation(deepcopy(line)), sort_keys=True) + "\n",
    )
    snapshot = _snapshot_at_current_head(workspace, root)

    with pytest.raises(MemError) as exc:
        replay_warnings(workspace, snapshot)
    assert exc.value.detail.code == "WORKSPACE_INTEGRITY_ERROR"


def test_replay_rejects_ledger_wide_duplicate_transaction_identity(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    root, _request = _committed_source_request(
        workspace, proposal_factory, business_markdown_factory
    )
    ledger = workspace.repository.root / "transactions/transactions.jsonl"
    line = ledger.read_text(encoding="utf-8").strip()
    _commit_corrupt_ledger(workspace, line + "\n" + line + "\n")
    snapshot = _snapshot_at_current_head(workspace, root)

    with pytest.raises(MemError) as exc:
        replay_warnings(workspace, snapshot)
    assert exc.value.detail.code == "WORKSPACE_INTEGRITY_ERROR"


@pytest.mark.parametrize("invalid", ["duplicate-key", "non-finite"])
def test_replay_rejects_noncanonical_json_at_any_nesting(
    workspace, proposal_factory, business_markdown_factory, invalid
) -> None:
    root, request = _committed_source_request(
        workspace, proposal_factory, business_markdown_factory
    )
    ledger = workspace.repository.root / "transactions/transactions.jsonl"
    line = ledger.read_text(encoding="utf-8").strip()
    if invalid == "duplicate-key":
        needle = f'"input_digest": "{request.receipt["input_digest"]}"'
        replacement = needle + ", " + needle
        assert needle in line
        line = line.replace(needle, replacement, 1)
    else:
        needle = f'"operation_count": {len(request.operations)}'
        assert needle in line
        line = line.replace(needle, '"operation_count": NaN', 1)
    _commit_corrupt_ledger(workspace, line + "\n")
    snapshot = _snapshot_at_current_head(workspace, root)

    with pytest.raises(MemError) as exc:
        replay_warnings(workspace, snapshot)
    assert exc.value.detail.code == "WORKSPACE_INTEGRITY_ERROR"
