from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import shutil

from mem_core.errors import MemError
import pytest

from pco.proposals.archive import archive_committed_proposal
from pco.proposals import archive as archive_module
from pco.proposals.models import (
    BusinessFile,
    ProposalArtifactMaterial,
    ProposalCommitReceipt,
    ProposalManifest,
)
from pco.proposals.service import ProposalService


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
    manifest_bytes = (
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True) + "\n"
    ).encode()
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
    root, receipt = _archive_fixture(
        workspace, proposal_factory, business_markdown_factory
    )

    result = archive_committed_proposal(workspace.paths, receipt)

    target = workspace.paths.proposals / ".committed" / receipt.receipt_id
    assert result.status == "archived"
    assert not root.exists()
    assert target.is_dir()
    assert archive_committed_proposal(workspace.paths, receipt).status == "archived"


def test_archive_reports_conflict_and_preserves_changed_active_tree(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    root, receipt = _archive_fixture(
        workspace, proposal_factory, business_markdown_factory
    )
    changed = root / "evidence/ev_archive.md"
    changed.write_text("changed after commit\n", encoding="utf-8")

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "conflict"
    assert changed.read_text(encoding="utf-8") == "changed after commit\n"
    assert not (workspace.paths.proposals / ".committed" / receipt.receipt_id).exists()


def test_archive_reports_missing_when_neither_path_exists(
    workspace, proposal_factory, business_markdown_factory
) -> None:
    root, receipt = _archive_fixture(
        workspace, proposal_factory, business_markdown_factory
    )
    for path in sorted(root.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    root.rmdir()

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "missing"


def test_archive_rename_failure_is_pending_and_preserves_active_tree(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    root, receipt = _archive_fixture(
        workspace, proposal_factory, business_markdown_factory
    )
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
    root, receipt = _archive_fixture(
        workspace, proposal_factory, business_markdown_factory
    )
    (root / "inputs/context.txt").unlink()
    (root / "inputs/context.txt").symlink_to(tmp_path / "outside")

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "conflict"
    assert root.is_dir()


def test_archive_does_not_rename_a_tree_replaced_after_hash_verification(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    root, receipt = _archive_fixture(
        workspace, proposal_factory, business_markdown_factory
    )
    real_capture = archive_module.capture_snapshot_from_root

    def capture_then_replace(*args, **kwargs):
        snapshot = real_capture(*args, **kwargs)
        displaced = root.with_name("review_displaced")
        root.rename(displaced)
        shutil.copytree(displaced, root)
        (root / "evidence/ev_archive.md").write_text(
            "attacker replacement\n", encoding="utf-8"
        )
        return snapshot

    monkeypatch.setattr(
        archive_module, "capture_snapshot_from_root", capture_then_replace
    )

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "conflict"
    assert root.is_dir()
    assert (root / "evidence/ev_archive.md").read_text(
        encoding="utf-8"
    ) == "attacker replacement\n"


def test_archive_recognizes_exact_target_if_rename_raises_after_move(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    root, receipt = _archive_fixture(
        workspace, proposal_factory, business_markdown_factory
    )
    real_rename = archive_module.atomic_rename

    def move_then_raise(*args, **kwargs):
        real_rename(*args, **kwargs)
        raise OSError("lost acknowledgement")

    monkeypatch.setattr(archive_module, "atomic_rename", move_then_raise)

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "archived"
    assert not root.exists()


def test_archive_ack_loss_rejects_receipt_exact_target_with_wrong_renamed_inode(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    root, receipt = _archive_fixture(
        workspace, proposal_factory, business_markdown_factory
    )
    target = workspace.paths.proposals / ".committed" / receipt.receipt_id
    displaced = target.with_name(target.name + "-displaced")
    real_rename = archive_module.atomic_rename

    def move_replace_and_raise(*args, **kwargs):
        real_rename(*args, **kwargs)
        target.rename(displaced)
        shutil.copytree(displaced, target)
        raise OSError("lost acknowledgement after target replacement")

    monkeypatch.setattr(archive_module, "atomic_rename", move_replace_and_raise)

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "conflict"
    assert displaced.is_dir()
    assert target.is_dir()


def test_archive_rename_error_after_active_change_is_conflict_not_pending(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    root, receipt = _archive_fixture(
        workspace, proposal_factory, business_markdown_factory
    )

    def change_then_raise(*_args, **_kwargs):
        (root / "evidence/ev_archive.md").write_text(
            "changed during rename\n", encoding="utf-8"
        )
        raise OSError("injected rename failure")

    monkeypatch.setattr(archive_module, "atomic_rename", change_then_raise)

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "conflict"
    assert root.is_dir()


def test_archive_final_anchored_recheck_rejects_same_inode_child_mutation(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    root, receipt = _archive_fixture(
        workspace, proposal_factory, business_markdown_factory
    )
    changed = root / "evidence/ev_archive.md"
    original_inode = changed.stat().st_ino
    real_rename = archive_module.atomic_rename

    def mutate_then_rename(*args, **kwargs):
        changed.write_text(
            "mutated after initial receipt verification\n", encoding="utf-8"
        )
        assert changed.stat().st_ino == original_inode
        return real_rename(*args, **kwargs)

    monkeypatch.setattr(archive_module, "atomic_rename", mutate_then_rename)

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "conflict"
    assert root.is_dir()
    assert not (workspace.paths.proposals / ".committed" / receipt.receipt_id).exists()


def test_archive_rejects_proposals_root_swap_at_atomic_boundary(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    root, receipt = _archive_fixture(
        workspace, proposal_factory, business_markdown_factory
    )
    proposals = workspace.paths.proposals
    displaced = proposals.with_name("PCO_PROPOSALS-displaced")
    real_rename = archive_module.atomic_rename

    def swap_root_then_rename(*args, **kwargs):
        proposals.rename(displaced)
        proposals.mkdir()
        return real_rename(*args, **kwargs)

    monkeypatch.setattr(archive_module, "atomic_rename", swap_root_then_rename)

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "conflict"
    assert (displaced / receipt.review_id).is_dir()
    assert not (displaced / ".committed" / receipt.receipt_id).exists()
    assert proposals.is_dir()


def test_archive_rejects_committed_parent_swap_at_atomic_boundary(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    root, receipt = _archive_fixture(
        workspace, proposal_factory, business_markdown_factory
    )
    committed = workspace.paths.proposals / ".committed"
    displaced = workspace.paths.proposals / ".committed-displaced"
    real_rename = archive_module.atomic_rename

    def swap_committed_then_rename(*args, **kwargs):
        committed.rename(displaced)
        committed.mkdir()
        return real_rename(*args, **kwargs)

    monkeypatch.setattr(archive_module, "atomic_rename", swap_committed_then_rename)

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "conflict"
    assert root.is_dir()
    assert not (displaced / receipt.receipt_id).exists()
    assert committed.is_dir()


def test_archive_rejects_symlinked_committed_root_even_if_target_looks_exact(
    workspace, proposal_factory, business_markdown_factory, tmp_path
) -> None:
    root, receipt = _archive_fixture(
        workspace, proposal_factory, business_markdown_factory
    )
    outside = tmp_path / "outside-archive"
    outside.mkdir()
    shutil.copytree(root, outside / receipt.receipt_id)
    (workspace.paths.proposals / ".committed").symlink_to(
        outside, target_is_directory=True
    )
    for path in sorted(root.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    root.rmdir()

    result = archive_committed_proposal(workspace.paths, receipt)

    assert result.status == "conflict"
    assert (outside / receipt.receipt_id).is_dir()


def test_atomic_archive_never_replaces_a_destination_created_during_rename(
    workspace, proposal_factory, business_markdown_factory, monkeypatch
) -> None:
    root, receipt = _archive_fixture(
        workspace, proposal_factory, business_markdown_factory
    )
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


@pytest.mark.parametrize("swap", ["proposals_root", "active"])
def test_abort_rejects_parent_or_active_swap_before_detach_without_deleting_either_tree(
    workspace, proposal_factory, monkeypatch, swap: str
) -> None:
    root = proposal_factory("review_abort_swap")
    original = root / "original.txt"
    original.write_text("original survives\n", encoding="utf-8")
    replacement_root = workspace.paths.proposals
    displaced = (
        replacement_root.with_name("PCO_PROPOSALS-displaced")
        if swap == "proposals_root"
        else root.with_name("review_abort_swap-displaced")
    )
    real_preflight = archive_module._preflight_safe_tree

    def preflight_then_swap(directory_fd, display):
        real_preflight(directory_fd, display)
        if swap == "proposals_root":
            replacement_root.rename(displaced)
            replacement_root.mkdir()
            replacement = replacement_root / root.name
        else:
            root.rename(displaced)
            replacement = root
        replacement.mkdir()
        (replacement / "replacement.txt").write_text(
            "replacement survives\n", encoding="utf-8"
        )

    monkeypatch.setattr(archive_module, "_preflight_safe_tree", preflight_then_swap)

    with pytest.raises(MemError) as exc:
        ProposalService(workspace).abort(root.name)

    assert exc.value.detail.code == "PROPOSAL_PATH_UNSAFE"
    original_after = (
        displaced / root.name / "original.txt"
        if swap == "proposals_root"
        else displaced / "original.txt"
    )
    assert original_after.read_text(encoding="utf-8") == "original survives\n"
    assert (replacement_root / root.name / "replacement.txt").read_text(
        encoding="utf-8"
    ) == "replacement survives\n"


def test_abort_post_preflight_race_does_not_partially_delete_active_tree(
    workspace, proposal_factory, tmp_path, monkeypatch
) -> None:
    root = proposal_factory("review_abort_race")
    safe = root / "a-safe.txt"
    raced = root / "z-raced.txt"
    safe.write_text("preserve me\n", encoding="utf-8")
    raced.write_text("replace me\n", encoding="utf-8")
    real_preflight = archive_module._preflight_safe_tree

    def preflight_then_replace(directory_fd, display):
        real_preflight(directory_fd, display)
        raced.unlink()
        raced.symlink_to(tmp_path / "outside")

    monkeypatch.setattr(archive_module, "_preflight_safe_tree", preflight_then_replace)

    with pytest.raises(MemError) as exc:
        ProposalService(workspace).abort(root.name)

    assert exc.value.detail.code == "PROPOSAL_PATH_UNSAFE"
    assert safe.read_text(encoding="utf-8") == "preserve me\n"
    assert raced.is_symlink()


def test_abort_cleanup_failure_after_detach_returns_success_with_exact_remnant(
    workspace, proposal_factory, monkeypatch
) -> None:
    root = proposal_factory("review_abort_cleanup")
    (root / "keep.txt").write_text("owned remnant\n", encoding="utf-8")
    canonical_head = workspace.repository.head()
    monkeypatch.setattr(
        archive_module,
        "_remove_tree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("injected cleanup failure")
        ),
    )

    result = ProposalService(workspace).abort(root.name)

    assert result["status"] == "aborted"
    warning = result["cleanup_warning"]
    assert warning["code"] == "PROPOSAL_ABORT_CLEANUP_PENDING"
    remnant = Path(warning["recovery_path"])
    assert remnant.is_dir()
    assert (remnant / "keep.txt").read_text(encoding="utf-8") == "owned remnant\n"
    assert not root.exists()
    assert workspace.repository.head() == canonical_head


def test_abort_never_implicitly_resumes_or_deletes_an_old_quarantine_remnant(
    workspace, proposal_factory, monkeypatch
) -> None:
    root = proposal_factory("review_abort_remnant")
    (root / "old.txt").write_text("old remnant\n", encoding="utf-8")
    real_remove = archive_module._remove_tree
    fail_once = True

    def first_cleanup_fails(*args, **kwargs):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("leave the first tombstone")
        return real_remove(*args, **kwargs)

    monkeypatch.setattr(archive_module, "_remove_tree", first_cleanup_fails)
    first = ProposalService(workspace).abort(root.name)
    old_remnant = Path(first["cleanup_warning"]["recovery_path"])
    assert (old_remnant / "old.txt").read_text(encoding="utf-8") == "old remnant\n"

    replacement = proposal_factory("review_abort_remnant")
    (replacement / "new.txt").write_text("new active\n", encoding="utf-8")
    second = ProposalService(workspace).abort(replacement.name)

    assert second == {"ok": True, "status": "aborted", "review_id": replacement.name}
    assert (old_remnant / "old.txt").read_text(encoding="utf-8") == "old remnant\n"


def test_abort_quarantine_parent_swap_before_detach_preserves_active_and_replacement(
    workspace, proposal_factory, monkeypatch
) -> None:
    root = proposal_factory("review_abort_quarantine_swap")
    original = root / "original.txt"
    original.write_text("original survives\n", encoding="utf-8")
    real_detach = archive_module._detach_active_noreplace
    quarantine = workspace.paths.tmp / "proposal-abort-quarantine"
    displaced = workspace.paths.tmp / "proposal-abort-quarantine-displaced"

    def swap_quarantine_then_detach(*args, **kwargs):
        quarantine.rename(displaced)
        quarantine.mkdir()
        (quarantine / "replacement.txt").write_text(
            "replacement survives\n", encoding="utf-8"
        )
        return real_detach(*args, **kwargs)

    monkeypatch.setattr(
        archive_module, "_detach_active_noreplace", swap_quarantine_then_detach
    )

    with pytest.raises(MemError) as exc:
        ProposalService(workspace).abort(root.name)

    assert exc.value.detail.code == "PROPOSAL_PATH_UNSAFE"
    assert original.read_text(encoding="utf-8") == "original survives\n"
    assert (quarantine / "replacement.txt").read_text(
        encoding="utf-8"
    ) == "replacement survives\n"


def test_abort_unsupported_atomic_detach_is_structured_and_non_destructive(
    workspace, proposal_factory, monkeypatch
) -> None:
    root = proposal_factory("review_abort_unsupported")
    original = root / "original.txt"
    original.write_text("still active\n", encoding="utf-8")
    monkeypatch.setattr(
        archive_module,
        "_detach_active_noreplace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.ENOTSUP, "atomic detach unavailable")
        ),
    )

    with pytest.raises(MemError) as exc:
        ProposalService(workspace).abort(root.name)

    assert exc.value.detail.code == "PROPOSAL_PATH_UNSAFE"
    assert exc.value.detail.phase == "proposal_abort"
    assert original.read_text(encoding="utf-8") == "still active\n"


def test_abort_non_utf8_entry_returns_structured_abort_error(
    workspace, proposal_factory
) -> None:
    root = proposal_factory("review_abort_non_utf8")
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        file_fd = os.open(
            b"bad-\xff",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory_fd,
        )
        os.close(file_fd)
    finally:
        os.close(directory_fd)

    with pytest.raises(MemError) as exc:
        ProposalService(workspace).abort(root.name)

    assert exc.value.detail.code == "PROPOSAL_PATH_UNSAFE"
    assert exc.value.detail.phase == "proposal_abort"
    assert exc.value.detail.canonical_changed is False
    assert exc.value.detail.retry_safe is False


def test_abort_discovery_errors_are_labeled_as_abort(
    workspace, proposal_factory
) -> None:
    proposal_factory("review_abort_expected")

    with pytest.raises(MemError) as exc:
        ProposalService(workspace).abort("review_abort_wrong")

    assert exc.value.detail.phase == "proposal_abort"
