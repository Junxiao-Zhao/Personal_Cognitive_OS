from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess

from mem_core.errors import MemError
from mem_core.profile import Profile
from mem_core.registry import default_registry
from mem_core.repository import MemoryRepository
import pytest

from pco.derivation_manifest import DerivationManifest
from pco import projections as projections_module
from pco.projections import build_projection, projection_path
from pco.proposals.markdown import render_business_markdown
from pco.proposals.models import BusinessFile, ProposalManifest
from pco.proposals.service import ProposalService
from pco.verify import verify_projection, verify_workspace

EXPECTED_FILES = {
    "evidence/ev_1.md",
    "sources/src_1.md",
    "events/evt_1.md",
    "psychologies/psy_1.md",
    "philosophies/phi_1.md",
    "archetypes/arc_1.md",
    "hypotheses/hyp_1.md",
    "META_MEMORY.md",
    "SESSION_HANDOFF.md",
}


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _commit_business_files(
    workspace,
    business_markdown_factory,
    *,
    review_id: str,
    documents: list[dict],
) -> None:
    root = workspace.paths.proposals / review_id
    root.mkdir()
    files = []
    for document in documents:
        relative = document["path"]
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            business_markdown_factory(
                object_type=document["type"],
                record_id=document["id"],
                operation=document.get("operation", "create"),
                base_revision=document.get("base_revision"),
                fields=document["fields"],
                body=document["body"],
            )
        )
        files.append(
            BusinessFile(path=relative, operation=document.get("operation", "create"))
        )
    manifest = ProposalManifest(
        review_id=review_id,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        files=files,
    )
    (root / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert (
        ProposalService(workspace).commit(review_id)["canonical_status"] == "committed"
    )


def test_projection_contains_every_current_effective_stream(
    committed_workspace, tmp_path: Path
) -> None:
    target = tmp_path / "projection"
    manifest = build_projection(
        committed_workspace.repository,
        target,
        committed_workspace.repository.head(),
    )

    assert set(manifest.files) == EXPECTED_FILES
    assert set(_tree_bytes(target)) == EXPECTED_FILES | {"manifest.json"}
    assert manifest.kind == "pco_memory"
    assert manifest.profile_version == "0.5.0"
    assert verify_projection(target)["ok"] is True


def test_projection_payload_is_the_canonical_business_markdown(
    committed_workspace, tmp_path: Path
) -> None:
    target = tmp_path / "projection"
    commit = committed_workspace.repository.head()
    build_projection(committed_workspace.repository, target, commit)
    record = committed_workspace.repository.current_records("hypotheses")["hyp_1"]

    assert (target / "hypotheses/hyp_1.md").read_bytes() == render_business_markdown(
        record
    )


def test_manifest_is_stable_strict_json_and_hashes_every_payload(
    committed_workspace, tmp_path: Path
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    commit = committed_workspace.repository.head()
    manifest = build_projection(committed_workspace.repository, first, commit)
    build_projection(committed_workspace.repository, second, commit)

    assert (first / "manifest.json").read_bytes() == (
        second / "manifest.json"
    ).read_bytes()
    raw = (first / "manifest.json").read_bytes()
    assert raw.endswith(b"\n")
    assert (
        raw
        == (
            json.dumps(
                manifest.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
    )
    assert "manifest.json" not in manifest.files
    for relative, digest in manifest.files.items():
        assert (
            digest
            == "sha256:" + hashlib.sha256((first / relative).read_bytes()).hexdigest()
        )


def test_build_uses_exact_commit_not_dirty_canonical_worktree(
    committed_workspace, tmp_path: Path
) -> None:
    commit = committed_workspace.repository.head()
    before = tmp_path / "before"
    after = tmp_path / "after"
    build_projection(committed_workspace.repository, before, commit)
    stream = committed_workspace.repository.root / "hypotheses/hypotheses.jsonl"
    stream.write_text("dirty non-json worktree bytes\n", encoding="utf-8")

    build_projection(committed_workspace.repository, after, commit)

    assert _tree_bytes(after) == _tree_bytes(before)


def test_build_does_not_consume_a_profile_reloaded_from_dirty_worktree(
    committed_workspace, tmp_path: Path
) -> None:
    commit = committed_workspace.repository.head()
    expected = tmp_path / "expected"
    actual = tmp_path / "actual"
    build_projection(committed_workspace.repository, expected, commit)
    profile_root = committed_workspace.repository.root / "profiles/pco"
    profile_path = profile_root / "profile.yaml"
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8").replace(
            "version: 0.5.0", "version: 9.9.9", 1
        ),
        encoding="utf-8",
    )
    dirty_repository = MemoryRepository(
        committed_workspace.repository.root,
        Profile.load(profile_root, default_registry()),
    )

    build_projection(dirty_repository, actual, commit)

    assert _tree_bytes(actual) == _tree_bytes(expected)


def test_build_remains_pinned_to_old_commit_after_head_evolves_through_proposal(
    committed_workspace, business_markdown_factory, tmp_path: Path
) -> None:
    old_commit = committed_workspace.repository.head()
    _commit_business_files(
        committed_workspace,
        business_markdown_factory,
        review_id="review_projection_revision",
        documents=[
            {
                "path": "hypotheses/hyp_1.md",
                "type": "hypothesis",
                "id": "hyp_1",
                "operation": "revise",
                "base_revision": 1,
                "fields": {
                    "change_summary": "Revise the hypothesis",
                    "confidence": 0.7,
                    "evidence_refs": ["evidence:ev_1"],
                    "counter_evidence_refs": [],
                    "status": "hypothesis",
                },
                "body": "The later canonical wording.",
            }
        ],
    )
    old_target = tmp_path / "old"
    current_target = tmp_path / "current"

    build_projection(committed_workspace.repository, old_target, old_commit)
    build_projection(
        committed_workspace.repository,
        current_target,
        committed_workspace.repository.head(),
    )

    assert (
        b"A reviewed canonical statement."
        in (old_target / "hypotheses/hyp_1.md").read_bytes()
    )
    assert (
        b"The later canonical wording."
        in (current_target / "hypotheses/hyp_1.md").read_bytes()
    )


def test_projection_omits_tombstones_but_keeps_latest_superseded_revision(
    committed_workspace, business_markdown_factory, tmp_path: Path
) -> None:
    _commit_business_files(
        committed_workspace,
        business_markdown_factory,
        review_id="review_projection_effective",
        documents=[
            {
                "path": "hypotheses/hyp_1.md",
                "type": "hypothesis",
                "id": "hyp_1",
                "operation": "retire",
                "base_revision": 1,
                "fields": {
                    "change_summary": "Retire the hypothesis",
                    "confidence": 0.45,
                    "evidence_refs": ["evidence:ev_1"],
                    "counter_evidence_refs": [],
                    "status": "tombstone",
                },
                "body": "This hypothesis is retired.",
            },
            {
                "path": "psychologies/psy_1.md",
                "type": "psychology",
                "id": "psy_1",
                "operation": "revise",
                "base_revision": 1,
                "fields": {
                    "change_summary": "Supersede the earlier concept wording",
                    "name": "Reflection revised",
                    "aliases": [],
                    "evidence_refs": ["evidence:ev_1"],
                    "status": "superseded",
                },
                "body": "The superseding psychology revision.",
            },
        ],
    )
    target = tmp_path / "projection"

    manifest = build_projection(
        committed_workspace.repository,
        target,
        committed_workspace.repository.head(),
    )

    assert "hypotheses/hyp_1.md" not in manifest.files
    assert (
        b"The superseding psychology revision."
        in (target / "psychologies/psy_1.md").read_bytes()
    )


def test_projection_selects_latest_effective_session_handoff(
    committed_workspace, business_markdown_factory, tmp_path: Path
) -> None:
    _commit_business_files(
        committed_workspace,
        business_markdown_factory,
        review_id="review_projection_handoff",
        documents=[
            {
                "path": "SESSION_HANDOFF.md",
                "type": "session_handoff",
                "id": "handoff_2",
                "fields": {
                    "change_summary": "Record a later handoff",
                    "topics": ["Later topic"],
                    "unanswered_questions": [],
                    "active_tensions": [],
                    "recent_decisions": [],
                    "next_directions": ["Continue later"],
                    "status": "active",
                },
                "body": "The latest unfinished context.",
            }
        ],
    )
    target = tmp_path / "projection"

    build_projection(
        committed_workspace.repository,
        target,
        committed_workspace.repository.head(),
    )

    assert b"id: handoff_2" in (target / "SESSION_HANDOFF.md").read_bytes()
    assert (
        b"The latest unfinished context."
        in (target / "SESSION_HANDOFF.md").read_bytes()
    )


def test_build_rejects_source_commit_profile_mismatch_without_target_mutation(
    committed_workspace, tmp_path: Path
) -> None:
    repository = committed_workspace.repository
    profile = repository.root / ".mem-profile.json"
    original = profile.read_bytes()
    marker = json.loads(original)
    marker["version"] = "9.9.9"
    profile.write_text(json.dumps(marker), encoding="utf-8")
    repository._git("add", ".mem-profile.json")
    repository._git("commit", "-m", "test mismatched profile marker", "--no-verify")
    target = tmp_path / "projection"

    with pytest.raises(MemError) as caught:
        build_projection(repository, target, repository.head())

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert not target.exists()


def test_build_rejects_same_repository_commit_outside_head_history(
    committed_workspace, tmp_path: Path
) -> None:
    repository = committed_workspace.repository
    tree = repository._git("rev-parse", "HEAD^{tree}")
    orphan = repository._git("commit-tree", tree, "-m", "orphan projection tree")
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", orphan, "HEAD"],
        cwd=repository.root,
        check=False,
    )
    assert ancestry.returncode == 1
    target = tmp_path / "projection"

    with pytest.raises(MemError) as caught:
        build_projection(repository, target, orphan)

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert not target.exists()


def test_git_launch_failure_at_commit_boundary_is_structured(
    committed_workspace, tmp_path: Path, monkeypatch
) -> None:
    commit = committed_workspace.repository.head()

    def missing_git(*_args, **_kwargs):
        raise FileNotFoundError("git missing")

    monkeypatch.setattr(projections_module.subprocess, "run", missing_git)
    target = tmp_path / "projection"

    with pytest.raises(MemError) as caught:
        build_projection(
            committed_workspace.repository,
            target,
            commit,
        )

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert caught.value.detail.canonical_changed is False
    assert not target.exists()


def test_nonzero_git_commit_probe_is_structured(
    committed_workspace, tmp_path: Path
) -> None:
    target = tmp_path / "projection"

    with pytest.raises(MemError) as caught:
        build_projection(committed_workspace.repository, target, "0" * 40)

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert not target.exists()


@pytest.mark.parametrize("existing", ["file", "nonempty", "symlink", "fifo"])
def test_builder_never_replaces_or_follows_existing_target(
    committed_workspace, tmp_path: Path, existing: str
) -> None:
    target = tmp_path / "projection"
    if existing == "file":
        target.write_text("keep\n", encoding="utf-8")
    elif existing == "nonempty":
        target.mkdir()
        (target / "keep.txt").write_text("keep\n", encoding="utf-8")
    else:
        if existing == "symlink":
            destination = tmp_path / "destination"
            destination.mkdir()
            target.symlink_to(destination, target_is_directory=True)
        else:
            os.mkfifo(target)
    before = _tree_bytes(tmp_path)

    with pytest.raises(MemError) as caught:
        build_projection(
            committed_workspace.repository,
            target,
            committed_workspace.repository.head(),
        )

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert _tree_bytes(tmp_path) == before


def test_empty_existing_real_target_is_accepted(
    committed_workspace, tmp_path: Path
) -> None:
    target = tmp_path / "projection"
    target.mkdir()

    build_projection(
        committed_workspace.repository,
        target,
        committed_workspace.repository.head(),
    )

    assert verify_projection(target)["ok"] is True


def test_absent_target_parent_swap_never_writes_through_replacement_symlink(
    committed_workspace, tmp_path: Path, monkeypatch
) -> None:
    parent = tmp_path / "declared"
    displaced = tmp_path / "declared-original"
    outside = tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    target = parent / "projection"
    real_lstat = os.lstat
    real_stat = os.stat
    swapped = False

    def swap_parent() -> None:
        nonlocal swapped
        if swapped:
            return
        parent.rename(displaced)
        parent.symlink_to(outside, target_is_directory=True)
        swapped = True

    def racing_lstat(path, *args, **kwargs):
        if Path(path) == target and not swapped:
            try:
                return real_lstat(path, *args, **kwargs)
            except FileNotFoundError:
                swap_parent()
                raise
        return real_lstat(path, *args, **kwargs)

    def racing_stat(path, *args, **kwargs):
        if path == target.name and kwargs.get("dir_fd") is not None and not swapped:
            swap_parent()
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(projections_module.os, "lstat", racing_lstat)
    monkeypatch.setattr(projections_module.os, "stat", racing_stat)

    with pytest.raises(MemError) as caught:
        build_projection(
            committed_workspace.repository,
            target,
            committed_workspace.repository.head(),
        )

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert parent.is_symlink()
    assert parent.resolve() == outside.resolve()
    assert not any(outside.iterdir())
    assert displaced.is_dir()


def test_builder_rejects_nonportable_target_name_without_mutation(
    committed_workspace, tmp_path: Path
) -> None:
    target = tmp_path / "bad\\name"

    with pytest.raises(MemError) as caught:
        build_projection(
            committed_workspace.repository,
            target,
            committed_workspace.repository.head(),
        )

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert not target.exists()


def test_verify_reports_exact_modified_missing_additional_and_stale_lists(
    committed_workspace, tmp_path: Path
) -> None:
    target = tmp_path / "projection"
    commit = committed_workspace.repository.head()
    build_projection(committed_workspace.repository, target, commit)
    (target / "hypotheses/hyp_1.md").write_text("changed\n", encoding="utf-8")
    (target / "events/evt_1.md").unlink()
    (target / "unmanaged.txt").write_text("extra\n", encoding="utf-8")

    result = verify_projection(target, expected_commit="f" * 40)

    assert result["ok"] is False
    assert result["status"] == "stale"
    assert result["missing"] == ["events/evt_1.md"]
    assert result["modified"] == ["hypotheses/hyp_1.md"]
    assert result["additional"] == ["unmanaged.txt"]
    assert result["source_commit"] == commit
    assert result["expected_commit"] == "f" * 40


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_initializer_empty_projection_is_missing_state(
    tmp_path: Path, missing: bool
) -> None:
    target = tmp_path / "PCO_MEMORY"
    if not missing:
        target.mkdir()
    before = _tree_bytes(tmp_path)

    result = verify_projection(target)

    assert result == {
        "ok": False,
        "status": "missing",
        "kind": "pco_memory",
        "source_commit": None,
        "expected_commit": None,
        "profile_version": None,
        "missing": ["manifest.json"],
        "modified": [],
        "additional": [],
    }
    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize(
    "manifest",
    [
        b"not json\n",
        b'{"kind":"pco_memory","kind":"retrieval_index"}\n',
        b'{"kind":"pco_memory","source_commit":"bad","profile_version":"0.5.0","files":{},"extra":1}\n',
    ],
)
def test_verify_rejects_malformed_or_ambiguous_manifest(
    tmp_path: Path, manifest: bytes
) -> None:
    target = tmp_path / "projection"
    target.mkdir()
    (target / "manifest.json").write_bytes(manifest)
    before = _tree_bytes(target)

    with pytest.raises(MemError) as caught:
        verify_projection(target)

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert _tree_bytes(target) == before


def test_verify_rejects_noncanonical_manifest_bytes_read_only(
    committed_workspace, tmp_path: Path
) -> None:
    target = tmp_path / "projection"
    build_projection(
        committed_workspace.repository,
        target,
        committed_workspace.repository.head(),
    )
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    before = _tree_bytes(target)

    with pytest.raises(MemError) as caught:
        verify_projection(target)

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert _tree_bytes(target) == before


@pytest.mark.parametrize("unsafe", ["symlink", "fifo"])
def test_verify_rejects_symlink_and_special_entries_without_reading_targets(
    committed_workspace, tmp_path: Path, unsafe: str
) -> None:
    target = tmp_path / "projection"
    build_projection(
        committed_workspace.repository,
        target,
        committed_workspace.repository.head(),
    )
    path = target / "unsafe"
    if unsafe == "symlink":
        secret = tmp_path / "secret"
        secret.write_text("do not read\n", encoding="utf-8")
        path.symlink_to(secret)
    else:
        os.mkfifo(path)
    before = _tree_bytes(target)

    with pytest.raises(MemError) as caught:
        verify_projection(target)

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert _tree_bytes(target) == before


def test_verify_returns_independent_results(
    committed_workspace, tmp_path: Path
) -> None:
    target = tmp_path / "projection"
    build_projection(
        committed_workspace.repository,
        target,
        committed_workspace.repository.head(),
    )
    first = verify_projection(target)
    first["missing"].append("invented")

    assert verify_projection(target)["missing"] == []


def test_verify_workspace_delegates_requested_projection_without_writes(
    committed_workspace,
) -> None:
    target = committed_workspace.paths.projection
    before = _tree_bytes(committed_workspace.root)

    result = verify_workspace(committed_workspace, target)

    assert result["projection"]["ok"] is True
    assert _tree_bytes(committed_workspace.root) == before


def test_manifest_model_forbids_wrong_kinds_extra_fields_and_bad_hashes() -> None:
    valid = {
        "kind": "pco_memory",
        "source_commit": "a" * 40,
        "profile_version": "0.5.0",
        "files": {"META_MEMORY.md": "sha256:" + "b" * 64},
    }
    DerivationManifest.model_validate(valid, strict=True)
    for change in (
        {"kind": "legacy"},
        {"source_commit": "HEAD"},
        {"files": {"../escape": "sha256:" + "b" * 64}},
        {"files": {"META_MEMORY.md": "b" * 64}},
        {"unknown": True},
    ):
        with pytest.raises(Exception):
            DerivationManifest.model_validate(valid | change, strict=True)


@pytest.mark.parametrize("length", [39, 41, 63, 65])
def test_manifest_and_builder_accept_only_exact_git_oid_lengths(
    committed_workspace, tmp_path: Path, length: int
) -> None:
    source_commit = "a" * length
    manifest = {
        "kind": "pco_memory",
        "source_commit": source_commit,
        "profile_version": "0.5.0",
        "files": {},
    }

    with pytest.raises(Exception):
        DerivationManifest.model_validate(manifest, strict=True)
    with pytest.raises(MemError) as caught:
        build_projection(
            committed_workspace.repository, tmp_path / "projection", source_commit
        )
    assert caught.value.detail.code == "DERIVATION_FAILED"


def test_projection_paths_encode_valid_unicode_colons_and_traversal_safely(
    committed_workspace, tmp_path: Path
) -> None:
    repository = committed_workspace.repository
    original = repository.root / "hypotheses/hypotheses.jsonl"
    record = deepcopy(repository.current_records("hypotheses")["hyp_1"])
    record["id"] = "../对象:一"
    record["revision"] = 1
    record["payload"]["previous_revision"] = None
    original.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    repository._git("add", "hypotheses/hypotheses.jsonl")
    repository._git("commit", "-m", "test unicode projection id", "--no-verify")
    target = tmp_path / "projection"

    manifest = build_projection(repository, target, repository.head())

    names = set(manifest.files) - {
        item for item in EXPECTED_FILES if item.startswith("hypotheses/")
    }
    hypothesis_paths = [item for item in names if item.startswith("hypotheses/")]
    assert len(hypothesis_paths) == 1
    assert ".." not in Path(hypothesis_paths[0]).parts
    assert ":" not in hypothesis_paths[0]
    assert (target / hypothesis_paths[0]).is_file()


def test_projection_path_maps_windows_reserved_profile_id_portably() -> None:
    relative = projection_path("events", "CON")

    assert relative.startswith("events/")
    assert Path(relative).stem.upper() != "CON"


def test_build_rejects_duplicate_keys_in_canonical_jsonl_without_target_mutation(
    committed_workspace, tmp_path: Path
) -> None:
    repository = committed_workspace.repository
    stream = repository.root / "hypotheses/hypotheses.jsonl"
    raw = stream.read_text(encoding="utf-8")
    stream.write_text(
        raw.replace('"id": "hyp_1"', '"id": "hyp_1", "id": "other"'), encoding="utf-8"
    )
    repository._git("add", "hypotheses/hypotheses.jsonl")
    repository._git("commit", "-m", "test ambiguous canonical JSON", "--no-verify")
    target = tmp_path / "projection"

    with pytest.raises(MemError) as caught:
        build_projection(repository, target, repository.head())

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert not target.exists()


def test_projection_rejects_platform_casefold_path_collision(
    committed_workspace, tmp_path: Path
) -> None:
    repository = committed_workspace.repository
    stream = repository.root / "hypotheses/hypotheses.jsonl"
    first = deepcopy(repository.current_records("hypotheses")["hyp_1"])
    first["id"] = "Case"
    second = deepcopy(first)
    second["id"] = "case"
    stream.write_text(
        "\n".join(json.dumps(item, sort_keys=True) for item in (first, second)) + "\n",
        encoding="utf-8",
    )
    repository._git("add", "hypotheses/hypotheses.jsonl")
    repository._git("commit", "-m", "test colliding projection ids", "--no-verify")
    target = tmp_path / "projection"

    with pytest.raises(MemError) as caught:
        build_projection(repository, target, repository.head())

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert not target.exists()
