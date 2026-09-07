from __future__ import annotations

import hashlib
import json
import shutil

from pco.proposals.archive import archive_committed_proposal
from pco.proposals import archive as archive_module
from pco.proposals.models import (
    BusinessFile,
    ProposalArtifactMaterial,
    ProposalCommitReceipt,
    ProposalManifest,
)


def _archive_fixture(workspace, proposal_factory, business_markdown_factory):
    root = proposal_factory("review_archive")
    (root / "evidence").mkdir()
    (root / "inputs").mkdir()
    business = business_markdown_factory(
        object_type="evidence",
        record_id="ev_archive",
        fields={
            "change_summary": "Archive reviewed evidence",
            "speaker": "user",
            "source_kind": "conversation",
            "user_authored": True,
            "fidelity": "selected_excerpt",
            "status": "active",
        },
    )
    artifact = b"review input\n"
    (root / "evidence/ev_archive.md").write_bytes(business)
    (root / "inputs/context.txt").write_bytes(artifact)
    content_hash = "sha256:" + hashlib.sha256(artifact).hexdigest()
    manifest = ProposalManifest(
        review_id=root.name,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        input_material=[
            ProposalArtifactMaterial(
                material_id="context",
                kind="proposal_artifact",
                path="inputs/context.txt",
                expected_content_hash=content_hash,
                fidelity="selected_excerpt",
            )
        ],
        files=[BusinessFile(path="evidence/ev_archive.md", operation="create")],
    )
    manifest_bytes = (json.dumps(manifest.model_dump(mode="json"), sort_keys=True) + "\n").encode()
    (root / "manifest.json").write_bytes(manifest_bytes)
    receipt = ProposalCommitReceipt(
        receipt_id="commit_" + "1" * 64,
        workspace_id=workspace.identity["workspace_id"],
        review_id=root.name,
        manifest_hash="sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
        files=[
            {
                "path": "evidence/ev_archive.md",
                "hash": "sha256:" + hashlib.sha256(business).hexdigest(),
            }
        ],
        input_material=[
            {
                "material_id": "context",
                "kind": "proposal_artifact",
                "path": "inputs/context.txt",
                "hash": content_hash,
                "file_hash": content_hash,
            }
        ],
        input_digest="sha256:" + "2" * 64,
        proposal_digest="sha256:" + "3" * 64,
        transaction_digest="sha256:" + "4" * 64,
        digest_controls={
            "profile_version": workspace.profile.version,
            "manifest_schema_version": manifest.schema_version,
            "stream_schema_versions": {
                name: stream.schema_version
                for name, stream in workspace.profile.config.streams.items()
            },
        },
        committed_at="2026-08-30T10:00:00+00:00",
    )
    return root, receipt


def test_archive_renames_only_an_exact_receipt_bound_tree(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    root, receipt = _archive_fixture(workspace, proposal_factory, business_markdown_factory)

    result = archive_committed_proposal(workspace.paths, receipt)

    target = workspace.paths.proposals / ".committed" / receipt.receipt_id
    assert result.status == "archived"
    assert not root.exists()
    assert target.is_dir()
    assert archive_committed_proposal(workspace.paths, receipt).status == "archived"


def test_archive_reports_conflict_and_preserves_changed_active_tree(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    root, receipt = _archive_fixture(workspace, proposal_factory, business_markdown_factory)
    changed = root / "evidence/ev_archive.md"
    changed.write_text("changed after commit\n", encoding="utf-8")

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "conflict"
    assert changed.read_text(encoding="utf-8") == "changed after commit\n"
    assert not (workspace.paths.proposals / ".committed" / receipt.receipt_id).exists()


def test_archive_reports_missing_when_neither_path_exists(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    root, receipt = _archive_fixture(workspace, proposal_factory, business_markdown_factory)
    for path in sorted(root.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    root.rmdir()

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "missing"


def test_archive_rename_failure_is_pending_and_preserves_active_tree(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    root, receipt = _archive_fixture(workspace, proposal_factory, business_markdown_factory)
    monkeypatch.setattr(
        "pco.proposals.archive.atomic_rename",
        lambda *_args: (_ for _ in ()).throw(OSError("injected")),
    )

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "pending"
    assert root.is_dir()


def test_archive_rejects_symlink_without_moving_anything(
    workspace, proposal_factory, business_markdown_factory, tmp_path
) -> None:
    root, receipt = _archive_fixture(workspace, proposal_factory, business_markdown_factory)
    (root / "inputs/context.txt").unlink()
    (root / "inputs/context.txt").symlink_to(tmp_path / "outside")

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "conflict"
    assert root.is_dir()


def test_archive_does_not_rename_a_tree_replaced_after_hash_verification(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    root, receipt = _archive_fixture(workspace, proposal_factory, business_markdown_factory)
    real_capture = archive_module.capture_snapshot_from_root

    def capture_then_replace(*args, **kwargs):
        snapshot = real_capture(*args, **kwargs)
        displaced = root.with_name("review_displaced")
        root.rename(displaced)
        shutil.copytree(displaced, root)
        (root / "evidence/ev_archive.md").write_text("attacker replacement\n", encoding="utf-8")
        return snapshot

    monkeypatch.setattr(archive_module, "capture_snapshot_from_root", capture_then_replace)

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "conflict"
    assert root.is_dir()
    assert (
        root / "evidence/ev_archive.md"
    ).read_text(encoding="utf-8") == "attacker replacement\n"


def test_archive_recognizes_exact_target_if_rename_raises_after_move(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    root, receipt = _archive_fixture(workspace, proposal_factory, business_markdown_factory)
    real_rename = archive_module.atomic_rename

    def move_then_raise(*args, **kwargs):
        real_rename(*args, **kwargs)
        raise OSError("lost acknowledgement")

    monkeypatch.setattr(archive_module, "atomic_rename", move_then_raise)

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "archived"
    assert not root.exists()


def test_archive_rename_error_after_active_change_is_conflict_not_pending(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    root, receipt = _archive_fixture(workspace, proposal_factory, business_markdown_factory)

    def change_then_raise(*_args, **_kwargs):
        (root / "evidence/ev_archive.md").write_text("changed during rename\n", encoding="utf-8")
        raise OSError("injected rename failure")

    monkeypatch.setattr(archive_module, "atomic_rename", change_then_raise)

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "conflict"
    assert root.is_dir()


def test_archive_rejects_symlinked_committed_root_even_if_target_looks_exact(
    workspace, proposal_factory, business_markdown_factory, tmp_path
) -> None:
    root, receipt = _archive_fixture(workspace, proposal_factory, business_markdown_factory)
    outside = tmp_path / "outside-archive"
    outside.mkdir()
    shutil.copytree(root, outside / receipt.receipt_id)
    (workspace.paths.proposals / ".committed").symlink_to(outside, target_is_directory=True)
    for path in sorted(root.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    root.rmdir()

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "conflict"
    assert (outside / receipt.receipt_id).is_dir()


def test_atomic_archive_never_replaces_a_destination_created_during_rename(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    root, receipt = _archive_fixture(workspace, proposal_factory, business_markdown_factory)
    target = workspace.paths.proposals / ".committed" / receipt.receipt_id
    real_rename = archive_module.atomic_rename

    def collide_then_rename(*args, **kwargs):
        target.mkdir()
        return real_rename(*args, **kwargs)

    monkeypatch.setattr(archive_module, "atomic_rename", collide_then_rename)

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "conflict"
    assert root.is_dir()
    assert target.is_dir()
    assert not list(target.iterdir())


def test_abort_preflights_entire_tree_before_deleting_any_entry(
    workspace, proposal_factory, tmp_path
) -> None:
    root = proposal_factory("review_unsafe_abort")
    safe = root / "a-safe.txt"
    safe.write_text("preserve me\n", encoding="utf-8")
    (root / "z-link").symlink_to(tmp_path / "outside")

    from mem_core.errors import MemError
    import pytest
    from pco.proposals.service import ProposalService

    with pytest.raises(MemError) as exc:
        ProposalService(workspace).abort("review_unsafe_abort")

    assert exc.value.detail.code == "PROPOSAL_PATH_UNSAFE"
    assert safe.read_text(encoding="utf-8") == "preserve me\n"
    assert (root / "z-link").is_symlink()
