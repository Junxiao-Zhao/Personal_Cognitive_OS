from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile

import pytest

from mem_core.errors import MemError
from pco.backlinks import build_reverse_references
from pco.derivation_manifest import (
    canonical_manifest_bytes,
    manifest_for_files,
    verify_derivation_tree,
)
from pco import retrieval
from pco import projections
from pco.retrieval import build_retrieval_generation, search


def _tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _layout(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {
        path.relative_to(root).as_posix() + ("/" if path.is_dir() else "")
        for path in root.rglob("*")
    }


def _rehash_generation(root: Path, *, profile_version: str | None = None) -> None:
    previous = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    payloads = _tree(root)
    payloads.pop("manifest.json")
    manifest = manifest_for_files(
        payloads,
        kind="retrieval_index",
        source_commit=previous["source_commit"],
        profile_version=profile_version or previous["profile_version"],
    )
    (root / "manifest.json").write_bytes(canonical_manifest_bytes(manifest))


@pytest.fixture
def stub_backends(monkeypatch):
    def tantivy(path: Path, documents: list[dict]) -> None:
        path.mkdir()
        (path / "meta.json").write_text(
            json.dumps({"count": len(documents)}, sort_keys=True), encoding="utf-8"
        )

    def milvus(path: Path, documents: list[dict]) -> None:
        path.write_text(
            json.dumps({"count": len(documents)}, sort_keys=True), encoding="utf-8"
        )

    monkeypatch.setattr(retrieval, "build_tantivy", tantivy)
    monkeypatch.setattr(retrieval, "build_milvus", milvus)


def test_build_generation_is_one_closed_exact_commit_tree(
    committed_workspace, tmp_path: Path, stub_backends
) -> None:
    repository = committed_workspace.repository
    commit = repository.head()
    target = tmp_path / "generation"

    manifest = build_retrieval_generation(repository, target, commit)

    assert manifest.kind == "retrieval_index"
    assert manifest.source_commit == commit
    assert set(_tree(target)) == set(manifest.files) | {"manifest.json"}
    assert {"documents.json", "reverse_refs.json", "milvus.db"} <= set(manifest.files)
    assert any(path.startswith("tantivy/") for path in manifest.files)
    assert (
        verify_derivation_tree(target, kind="retrieval_index", expected_commit=commit)[
            "ok"
        ]
        is True
    )
    assert not (target / "active.json").exists()
    assert not (target / "generations").exists()


def test_build_generation_rejects_existing_target_without_mutation(
    committed_workspace, tmp_path: Path, stub_backends
) -> None:
    target = tmp_path / "generation"
    target.mkdir()
    sentinel = target / "owned.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(MemError) as caught:
        build_retrieval_generation(
            committed_workspace.repository,
            target,
            committed_workspace.repository.head(),
        )

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert _tree(target) == {"owned.txt": b"keep"}


def test_backend_build_failure_is_structured_and_never_publishes(
    committed_workspace, tmp_path: Path, monkeypatch
) -> None:
    def fail(_path: Path, _documents: list[dict]) -> None:
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(retrieval, "build_tantivy", fail)
    target = tmp_path / "generation"

    with pytest.raises(MemError) as caught:
        build_retrieval_generation(
            committed_workspace.repository,
            target,
            committed_workspace.repository.head(),
        )

    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert caught.value.detail.canonical_changed is False
    assert not (target / "manifest.json").exists()


def test_missing_generation_uses_bounded_canonical_fallback_without_writes(
    committed_workspace, tmp_path: Path, monkeypatch
) -> None:
    missing = tmp_path / "missing"
    monkeypatch.setattr(retrieval, "CANONICAL_FALLBACK_CANDIDATES", 5)
    before = _tree(committed_workspace.paths.canonical)

    result = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=missing,
        query="reviewed",
        mode="current",
        limit=3,
    )

    assert result["derivation_status"] == "fallback"
    assert result["candidate_count"] <= 5
    assert len(result["items"]) <= 3
    assert not missing.exists()
    assert _tree(committed_workspace.paths.canonical) == before


def test_current_excludes_tombstones_while_historical_keeps_them(
    workspace_with_tombstone,
) -> None:
    current = search(
        repo_root=workspace_with_tombstone.paths.canonical,
        indexes_root=workspace_with_tombstone.root / "missing-index",
        query="hypothesis",
        mode="current",
    )
    historical = search(
        repo_root=workspace_with_tombstone.paths.canonical,
        indexes_root=workspace_with_tombstone.root / "missing-index",
        query="hypothesis",
        mode="historical",
    )

    assert all(item.get("status") != "tombstone" for item in current["items"])
    assert any(item.get("status") == "tombstone" for item in historical["items"])


def test_continuity_returns_only_latest_effective_session_handoff(
    workspace_with_handoffs,
) -> None:
    result = search(
        repo_root=workspace_with_handoffs.paths.canonical,
        indexes_root=workspace_with_handoffs.root / "missing-index",
        query="",
        mode="continuity",
    )

    assert [(item["stream"], item["id"]) for item in result["items"]] == [
        ("session_handoffs", "handoff_2")
    ]


@pytest.mark.parametrize("mode", ["current", "historical", "change", "pattern"])
def test_search_modes_are_deterministic_and_results_are_independent(
    committed_workspace, mode: str
) -> None:
    arguments = {
        "repo_root": committed_workspace.paths.canonical,
        "indexes_root": committed_workspace.root / "missing-index",
        "query": "reviewed",
        "mode": mode,
        "limit": 20,
    }
    first = search(**arguments)
    original = json.loads(json.dumps(first))
    if first["items"]:
        first["items"][0]["text"] = "mutated"

    second = search(**arguments)

    assert second == original


def test_search_rejects_bad_mode_limit_query_and_date_range(
    committed_workspace,
) -> None:
    base = {
        "repo_root": committed_workspace.paths.canonical,
        "indexes_root": committed_workspace.root / "missing-index",
    }
    for arguments in (
        {"mode": "unknown"},
        {"limit": 0},
        {"limit": True},
        {"query": None},
        {"start": "not-a-date"},
        {"start": "2026-09-02", "end": "2026-09-01"},
    ):
        with pytest.raises(ValueError):
            search(**base, **arguments)


def test_verified_generation_is_used_and_corruption_falls_back(
    committed_workspace, tmp_path: Path, stub_backends, monkeypatch
) -> None:
    target = tmp_path / "generation"
    commit = committed_workspace.repository.head()
    build_retrieval_generation(committed_workspace.repository, target, commit)
    monkeypatch.setattr(
        retrieval,
        "query_backends",
        lambda *_args, **_kwargs: ({}, {}),
    )

    current = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=target,
        query="reviewed",
    )
    assert current["derivation_status"] == "current"

    (target / "documents.json").write_text("[]", encoding="utf-8")
    fallback = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=target,
        query="reviewed",
    )
    assert fallback["derivation_status"] == "fallback"


def test_backend_query_failure_and_post_read_mutation_fall_back(
    committed_workspace, tmp_path: Path, stub_backends, monkeypatch
) -> None:
    target = tmp_path / "generation"
    build_retrieval_generation(
        committed_workspace.repository, target, committed_workspace.repository.head()
    )
    before = _tree(target)
    layout_before = _layout(target)
    query_roots: list[Path] = []

    def mutate_then_fail(generation, *_args, **_kwargs):
        query_roots.append(generation)
        (generation / "documents.json").write_text("[]", encoding="utf-8")
        raise RuntimeError("query failed")

    monkeypatch.setattr(retrieval, "query_backends", mutate_then_fail)
    result = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=target,
        query="reviewed",
    )

    assert result["derivation_status"] == "fallback"
    assert result["candidate_count"] > 0
    assert _tree(target) == before
    assert _layout(target) == layout_before
    assert len(query_roots) == 1
    assert not query_roots[0].exists()


def test_reverse_references_are_pure_sorted_and_include_all_reference_fields() -> None:
    documents = [
        {
            "stream": "hypotheses",
            "id": "hyp_b",
            "revision": 2,
            "evidence_refs": ["evidence:ev_2"],
            "counter_evidence_refs": ["evidence:ev_1"],
            "links": {},
        },
        {
            "stream": "events",
            "id": "evt_a",
            "revision": 1,
            "evidence_refs": ["evidence:ev_1"],
            "counter_evidence_refs": [],
            "links": {"psychologies": ["psy_1"]},
        },
    ]
    original = json.loads(json.dumps(documents))

    result = build_reverse_references(documents)

    assert documents == original
    assert result["evidence:ev_1"] == [
        {
            "source_id": "evt_a",
            "source_revision": 1,
            "source_stream": "events",
            "relation": "evidence_refs",
        },
        {
            "source_id": "hyp_b",
            "source_revision": 2,
            "source_stream": "hypotheses",
            "relation": "counter_evidence_refs",
        },
    ]
    assert result["psy_1"][0]["relation"] == "psychologies"


@pytest.mark.parametrize("damage", ["malformed", "additional", "symlink"])
def test_broken_generation_falls_back_without_touching_external_targets(
    committed_workspace, tmp_path: Path, damage: str
) -> None:
    generation = tmp_path / "generation"
    generation.mkdir()
    external = tmp_path / "external"
    external.write_text("do not read or write", encoding="utf-8")
    if damage == "malformed":
        (generation / "manifest.json").write_text("{", encoding="utf-8")
    elif damage == "additional":
        (generation / "unmanaged").write_text("x", encoding="utf-8")
    else:
        (generation / "manifest.json").symlink_to(external)
    external_before = external.read_bytes()

    result = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=generation,
        query="reviewed",
    )

    assert result["derivation_status"] == "fallback"
    assert external.read_bytes() == external_before


def test_stale_generation_falls_back_without_querying_backends(
    committed_workspace, tmp_path: Path, stub_backends, monkeypatch
) -> None:
    generation = tmp_path / "generation"
    build_retrieval_generation(
        committed_workspace.repository,
        generation,
        committed_workspace.repository.head(),
    )
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_commit"] = "0" * 40
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        retrieval,
        "query_backends",
        lambda *_args, **_kwargs: pytest.fail("stale generation was queried"),
    )

    result = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=generation,
        query="reviewed",
    )

    assert result["derivation_status"] == "fallback"


def test_fallback_candidate_cap_applies_before_ranking(
    committed_workspace, monkeypatch
) -> None:
    monkeypatch.setattr(retrieval, "CANONICAL_FALLBACK_CANDIDATES", 3)

    result = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=committed_workspace.root / "missing-index",
        query="reviewed",
        mode="historical",
        limit=10,
    )

    assert result["candidate_count"] == 3
    assert len(result["items"]) == 3


def test_tied_ranking_uses_ascending_utf8_identity_order(committed_workspace) -> None:
    documents = [
        {
            "stream": stream,
            "id": record_id,
            "revision": 1,
            "text": "same",
            "recorded_at": "2026-08-30T10:00:00Z",
            "occurred_at": "2026-08-30T10:00:00Z",
        }
        for stream, record_id in (
            ("psychologies", "psy_z"),
            ("archetypes", "arc_a"),
            ("events", "evt_m"),
        )
    ]

    items = retrieval._rank(documents, {}, "does-not-match", "current", 20, ({}, {}))

    identities = [(item["stream"], item["id"], item["revision"]) for item in items]
    assert identities == sorted(
        identities, key=lambda item: (item[0].encode(), item[1].encode(), item[2])
    )


def test_date_filters_are_structured_and_inclusive(committed_workspace) -> None:
    result = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=committed_workspace.root / "missing-index",
        mode="historical",
        start="2026-08-30T00:00:00Z",
        end="2026-08-30T23:59:59Z",
        limit=20,
    )

    assert result["items"]
    assert all(item["occurred_at"].startswith("2026-08-30") for item in result["items"])


def test_pattern_uses_internal_reverse_reference_graph() -> None:
    documents = [
        {
            "stream": "psychologies",
            "id": "psy_1",
            "revision": 1,
            "text": "Reflection",
            "recorded_at": "2026-08-30T10:00:00Z",
        },
        {
            "stream": "events",
            "id": "evt_1",
            "revision": 1,
            "text": "Event",
            "recorded_at": "2026-08-30T10:00:00Z",
        },
    ]
    reverse_refs = {
        "psy_1": [
            {
                "source_id": "evt_1",
                "source_revision": 1,
                "source_stream": "events",
                "relation": "psychologies",
            }
        ]
    }

    items = retrieval._rank(
        documents, reverse_refs, "Reflection", "pattern", 20, ({}, {})
    )
    by_id = {item["id"]: item for item in items}

    assert by_id["psy_1"]["score"] >= 1
    assert by_id["evt_1"]["score"] == 0.25


def test_build_is_pinned_to_commit_despite_dirty_worktree(
    committed_workspace, tmp_path: Path, stub_backends
) -> None:
    repository = committed_workspace.repository
    commit = repository.head()
    hypothesis_path = repository.root / "hypotheses/hypotheses.jsonl"
    original = hypothesis_path.read_bytes()
    hypothesis_path.write_bytes(original + b'{"dirty":true}\n')
    target = tmp_path / "generation"

    build_retrieval_generation(repository, target, commit)
    documents = json.loads((target / "documents.json").read_text(encoding="utf-8"))

    assert all(document.get("id") != "dirty" for document in documents)


def test_builder_rejects_symlink_target_and_backend_symlink_output(
    committed_workspace, tmp_path: Path, monkeypatch
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    symlink_target = tmp_path / "symlink-generation"
    symlink_target.symlink_to(external, target_is_directory=True)
    with pytest.raises(MemError):
        build_retrieval_generation(
            committed_workspace.repository,
            symlink_target,
            committed_workspace.repository.head(),
        )
    assert _tree(external) == {}

    def symlink_backend(path: Path, _documents: list[dict]) -> None:
        path.symlink_to(external, target_is_directory=True)

    monkeypatch.setattr(retrieval, "build_tantivy", symlink_backend)
    monkeypatch.setattr(
        retrieval,
        "build_milvus",
        lambda path, _documents: path.write_bytes(b"milvus"),
    )
    with pytest.raises(MemError) as caught:
        build_retrieval_generation(
            committed_workspace.repository,
            tmp_path / "backend-symlink",
            committed_workspace.repository.head(),
        )
    assert caught.value.detail.code == "DERIVATION_FAILED"


def test_backend_environment_is_restored_even_after_failure(monkeypatch) -> None:
    monkeypatch.setenv("NO_PROXY", "example.test")
    monkeypatch.delenv("no_proxy", raising=False)

    with pytest.raises(RuntimeError):
        with retrieval._local_backend_environment():
            assert "localhost" in os.environ["NO_PROXY"]
            assert "127.0.0.1" in os.environ["no_proxy"]
            raise RuntimeError("stop")

    assert os.environ["NO_PROXY"] == "example.test"
    assert "no_proxy" not in os.environ


def test_successful_generation_search_is_byte_stable_and_deeply_independent(
    committed_workspace, tmp_path: Path, stub_backends, monkeypatch
) -> None:
    generation = tmp_path / "generation"
    build_retrieval_generation(
        committed_workspace.repository,
        generation,
        committed_workspace.repository.head(),
    )
    monkeypatch.setattr(retrieval, "query_backends", lambda *_args, **_kwargs: ({}, {}))
    before = _tree(generation)

    first = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=generation,
        query="reviewed",
        limit=20,
    )
    first["items"][0]["links"]["mutated"] = ["x"]
    second = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=generation,
        query="reviewed",
        limit=20,
    )

    assert _tree(generation) == before
    assert all("mutated" not in item["links"] for item in second["items"])


def test_search_reads_captured_commit_not_dirty_profile_worktree(
    committed_workspace,
) -> None:
    profile_path = committed_workspace.paths.canonical / "profiles/pco/profile.yaml"
    profile_path.write_text("not: the canonical profile\n", encoding="utf-8")

    result = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=committed_workspace.root / "missing-index",
        query="reviewed",
    )

    assert result["derivation_status"] == "fallback"
    assert result["items"]


def test_fallback_does_not_recapture_head_after_initial_snapshot(
    committed_workspace, monkeypatch
) -> None:
    original = projections._git_probe

    def forbid_second_head(repository, *arguments):
        if arguments == ("rev-parse", "--verify", "HEAD^{commit}"):
            raise AssertionError("search recaptured mutable HEAD")
        return original(repository, *arguments)

    monkeypatch.setattr(projections, "_git_probe", forbid_second_head)

    result = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=committed_workspace.root / "missing-index",
        query="reviewed",
    )

    assert result["source_commit"] == committed_workspace.repository.head()
    assert result["items"]


def test_backend_memerror_is_normalized_to_derivation_failed(
    committed_workspace, tmp_path: Path, monkeypatch
) -> None:
    def fail(_path: Path, _documents: list[dict]) -> None:
        raise MemError(
            "INDEX_BACKEND_FAILED",
            "index",
            "foreign backend failure",
            canonical_changed=False,
            retry_safe=True,
            recovery=["retry"],
        )

    monkeypatch.setattr(retrieval, "build_tantivy", fail)

    with pytest.raises(MemError) as caught:
        build_retrieval_generation(
            committed_workspace.repository,
            tmp_path / "generation",
            committed_workspace.repository.head(),
        )

    assert caught.value.detail.code == "DERIVATION_FAILED"


def test_empty_owned_target_is_accepted(
    committed_workspace, tmp_path, stub_backends
) -> None:
    target = tmp_path / "generation"
    target.mkdir(mode=0o700)

    manifest = build_retrieval_generation(
        committed_workspace.repository,
        target,
        committed_workspace.repository.head(),
    )

    assert manifest.kind == "retrieval_index"


@pytest.mark.parametrize("binding", ["parent", "target"])
def test_builder_rejects_rebinding_after_base_payloads_without_writing_replacement(
    committed_workspace, tmp_path: Path, stub_backends, monkeypatch, binding: str
) -> None:
    parent = tmp_path / "caller"
    parent.mkdir()
    target = parent / "generation"
    displaced = tmp_path / f"displaced-{binding}"
    original = retrieval._write_new_tree
    swapped = False

    def write_then_rebind(path: Path, files: dict[str, bytes]):
        nonlocal swapped
        identity = original(path, files)
        if binding == "parent":
            parent.rename(displaced)
            parent.mkdir()
            target.mkdir()
        else:
            target.rename(displaced)
            target.mkdir()
        swapped = True
        return identity

    monkeypatch.setattr(retrieval, "_write_new_tree", write_then_rebind)

    with pytest.raises(MemError) as caught:
        build_retrieval_generation(
            committed_workspace.repository,
            target,
            committed_workspace.repository.head(),
        )

    assert swapped is True
    assert caught.value.detail.code == "DERIVATION_FAILED"
    assert _tree(target) == {}


@pytest.mark.parametrize("required", ["documents.json", "reverse_refs.json"])
def test_builder_never_shrinks_manifest_when_required_payload_disappears(
    committed_workspace, tmp_path: Path, monkeypatch, required: str
) -> None:
    def tantivy(path: Path, _documents: list[dict]) -> None:
        (path.parent / required).unlink()
        path.mkdir()
        (path / "meta").write_bytes(b"tantivy")

    monkeypatch.setattr(retrieval, "build_tantivy", tantivy)
    monkeypatch.setattr(
        retrieval,
        "build_milvus",
        lambda path, _documents: path.write_bytes(b"milvus"),
    )

    with pytest.raises(MemError) as caught:
        build_retrieval_generation(
            committed_workspace.repository,
            tmp_path / f"missing-{required}",
            committed_workspace.repository.head(),
        )

    assert caught.value.detail.code == "DERIVATION_FAILED"


def test_builder_requires_both_backend_payloads(
    committed_workspace, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(retrieval, "build_tantivy", lambda *_args: None)
    monkeypatch.setattr(retrieval, "build_milvus", lambda *_args: None)

    with pytest.raises(MemError) as caught:
        build_retrieval_generation(
            committed_workspace.repository,
            tmp_path / "missing-backends",
            committed_workspace.repository.head(),
        )

    assert caught.value.detail.code == "DERIVATION_FAILED"


def test_search_falls_back_when_generation_root_is_rebound_after_query(
    committed_workspace, tmp_path: Path, stub_backends, monkeypatch
) -> None:
    generation = tmp_path / "generation"
    clone = tmp_path / "clone"
    displaced = tmp_path / "displaced"
    build_retrieval_generation(
        committed_workspace.repository,
        generation,
        committed_workspace.repository.head(),
    )
    shutil.copytree(generation, clone)

    def swap_root(*_args, **_kwargs):
        generation.rename(displaced)
        clone.rename(generation)
        return {}, {}

    monkeypatch.setattr(retrieval, "query_backends", swap_root)

    result = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=generation,
        query="reviewed",
    )

    assert result["derivation_status"] == "fallback"
    assert result["items"]


def test_search_falls_back_when_generation_member_is_rebound_with_same_bytes(
    committed_workspace, tmp_path: Path, stub_backends, monkeypatch
) -> None:
    generation = tmp_path / "generation"
    build_retrieval_generation(
        committed_workspace.repository,
        generation,
        committed_workspace.repository.head(),
    )

    def swap_member(*_args, **_kwargs):
        member = generation / "documents.json"
        content = member.read_bytes()
        member.rename(generation / "documents.old")
        member.write_bytes(content)
        (generation / "documents.old").unlink()
        return {}, {}

    monkeypatch.setattr(retrieval, "query_backends", swap_member)

    result = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=generation,
        query="reviewed",
    )

    assert result["derivation_status"] == "fallback"
    assert result["items"]


@pytest.mark.parametrize(
    "damage",
    ["bad_text_type", "unknown_stream", "duplicate_key", "wrong_current"],
)
def test_manifest_valid_noncanonical_documents_use_fallback(
    committed_workspace, tmp_path: Path, stub_backends, monkeypatch, damage: str
) -> None:
    generation = tmp_path / f"generation-{damage}"
    build_retrieval_generation(
        committed_workspace.repository,
        generation,
        committed_workspace.repository.head(),
    )
    documents = json.loads((generation / "documents.json").read_text(encoding="utf-8"))
    if damage == "bad_text_type":
        documents[0]["text"] = 7
    elif damage == "unknown_stream":
        documents[0]["stream"] = "messages"
    elif damage == "duplicate_key":
        documents.append(dict(documents[0]))
    else:
        current = next(document for document in documents if document["current"])
        current["current"] = False
    (generation / "documents.json").write_text(
        json.dumps(documents, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    _rehash_generation(generation)
    monkeypatch.setattr(retrieval, "query_backends", lambda *_args: ({}, {}))

    result = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=generation,
        query="reviewed",
        mode="historical",
        limit=20,
    )

    assert result["derivation_status"] == "fallback"
    assert result["items"]


def test_manifest_valid_inconsistent_reverse_refs_use_fallback(
    committed_workspace, tmp_path: Path, stub_backends, monkeypatch
) -> None:
    generation = tmp_path / "generation-bad-refs"
    build_retrieval_generation(
        committed_workspace.repository,
        generation,
        committed_workspace.repository.head(),
    )
    (generation / "reverse_refs.json").write_text("{}\n", encoding="utf-8")
    _rehash_generation(generation)
    monkeypatch.setattr(retrieval, "query_backends", lambda *_args: ({}, {}))

    result = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=generation,
        query="reviewed",
        mode="pattern",
    )

    assert result["derivation_status"] == "fallback"
    assert result["items"]


def test_manifest_profile_mismatch_uses_fallback(
    committed_workspace, tmp_path: Path, stub_backends, monkeypatch
) -> None:
    generation = tmp_path / "generation-profile-mismatch"
    build_retrieval_generation(
        committed_workspace.repository,
        generation,
        committed_workspace.repository.head(),
    )
    _rehash_generation(generation, profile_version="0.5.999")
    monkeypatch.setattr(retrieval, "query_backends", lambda *_args: ({}, {}))

    result = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=generation,
        query="reviewed",
    )

    assert result["derivation_status"] == "fallback"
    assert result["items"]


def test_generation_ranking_failure_stays_inside_fallback_boundary(
    committed_workspace, tmp_path: Path, stub_backends, monkeypatch
) -> None:
    generation = tmp_path / "generation-rank-failure"
    build_retrieval_generation(
        committed_workspace.repository,
        generation,
        committed_workspace.repository.head(),
    )
    original = retrieval._rank
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TypeError("generation ranking failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(retrieval, "query_backends", lambda *_args: ({}, {}))
    monkeypatch.setattr(retrieval, "_rank", fail_once)

    result = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=generation,
        query="reviewed",
    )

    assert result["derivation_status"] == "fallback"
    assert result["items"]


def test_backend_query_consumes_private_snapshot_during_replace_and_restore(
    committed_workspace, tmp_path: Path, stub_backends, monkeypatch
) -> None:
    generation = tmp_path / "generation-private-query"
    build_retrieval_generation(
        committed_workspace.repository,
        generation,
        committed_workspace.repository.head(),
    )
    generation_before = _tree(generation)
    generation_layout_before = _layout(generation)
    canonical_before = _tree(committed_workspace.paths.canonical)
    canonical_layout_before = _layout(committed_workspace.paths.canonical)
    workspace_tmp_before = _tree(committed_workspace.paths.tmp)
    workspace_tmp_layout_before = _layout(committed_workspace.paths.tmp)
    environment_before = dict(os.environ)
    observed: dict[str, object] = {}

    def replace_query_restore(query_root: Path, *_args, **_kwargs):
        published = generation / "tantivy/meta.json"
        retained = generation / "tantivy/meta.retained"
        published.rename(retained)
        published.write_bytes(b"transient replacement")
        try:
            observed["consumed"] = (query_root / "tantivy/meta.json").read_bytes()
            observed["same_root"] = os.path.samefile(query_root, generation)
            observed["query_files"] = sorted(_tree(query_root))
            observed["query_root"] = query_root
        finally:
            published.unlink()
            retained.rename(published)
        return {}, {}

    monkeypatch.setattr(retrieval, "query_backends", replace_query_restore)

    result = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=generation,
        query="reviewed",
    )

    assert result["derivation_status"] == "current"
    assert observed["consumed"] == generation_before["tantivy/meta.json"]
    assert observed["same_root"] is False
    assert observed["query_files"] == ["tantivy/meta.json"]
    assert not Path(observed["query_root"]).exists()
    assert _tree(generation) == generation_before
    assert _layout(generation) == generation_layout_before
    assert _tree(committed_workspace.paths.canonical) == canonical_before
    assert _layout(committed_workspace.paths.canonical) == canonical_layout_before
    assert _tree(committed_workspace.paths.tmp) == workspace_tmp_before
    assert _layout(committed_workspace.paths.tmp) == workspace_tmp_layout_before
    assert dict(os.environ) == environment_before


def test_private_query_copy_ignores_workspace_tmpdir_and_unsafe_parent_falls_back(
    committed_workspace, tmp_path: Path, stub_backends, monkeypatch
) -> None:
    generation = tmp_path / "generation-external-query-copy"
    build_retrieval_generation(
        committed_workspace.repository,
        generation,
        committed_workspace.repository.head(),
    )
    workspace = committed_workspace.paths.root.resolve()
    hostile_tmp = committed_workspace.paths.tmp.resolve()
    before = _tree(workspace)
    layout_before = _layout(workspace)
    observed: list[Path] = []

    def observe_root(query_root: Path, *_args, **_kwargs):
        observed.append(query_root.resolve())
        assert (query_root / "tantivy/meta.json").read_bytes() == b'{"count": 9}'
        return {}, {}

    previous_tempdir = tempfile.tempdir
    try:
        monkeypatch.setenv("TMPDIR", str(hostile_tmp))
        tempfile.tempdir = None
        monkeypatch.setattr(retrieval, "query_backends", observe_root)

        result = search(
            repo_root=committed_workspace.paths.canonical,
            indexes_root=generation,
            query="reviewed",
        )
    finally:
        tempfile.tempdir = previous_tempdir

    assert result["derivation_status"] == "current"
    assert len(observed) == 1
    assert not observed[0].is_relative_to(workspace)
    assert not observed[0].is_relative_to(committed_workspace.paths.canonical.resolve())
    assert not observed[0].is_relative_to(generation.resolve())
    assert _tree(workspace) == before
    assert _layout(workspace) == layout_before

    monkeypatch.setattr(retrieval, "_SYSTEM_TEMP_PARENT", hostile_tmp, raising=False)
    observed.clear()
    fallback = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=generation,
        query="reviewed",
    )

    assert fallback["derivation_status"] == "fallback"
    assert observed == []
    assert _tree(workspace) == before
    assert _layout(workspace) == layout_before


@pytest.mark.parametrize("backend_raises", [False, True])
def test_private_query_cleanup_is_fd_bound_and_preserves_parent_replacement(
    committed_workspace,
    tmp_path: Path,
    stub_backends,
    monkeypatch,
    backend_raises: bool,
) -> None:
    generation = tmp_path / f"generation-parent-rebind-{backend_raises}"
    build_retrieval_generation(
        committed_workspace.repository,
        generation,
        committed_workspace.repository.head(),
    )
    original_payload = (generation / "tantivy/meta.json").read_bytes()
    environment_before = dict(os.environ)
    observed: dict[str, object] = {}

    def rebind_parent(query_root: Path, *_args, **_kwargs):
        real_root = query_root.resolve()
        parent = real_root.parent
        retained = parent.with_name(parent.name + "-retained")
        parent.rename(retained)
        parent.mkdir()
        sentinel = parent / "replacement.txt"
        sentinel.write_bytes(b"replacement must survive")
        observed.update(parent=parent, retained=retained, sentinel=sentinel)
        observed["consumed"] = (query_root / "tantivy/meta.json").read_bytes()
        if backend_raises:
            raise RuntimeError("backend failed after parent rebind")
        return {}, {}

    monkeypatch.setattr(retrieval, "query_backends", rebind_parent)
    try:
        result = search(
            repo_root=committed_workspace.paths.canonical,
            indexes_root=generation,
            query="reviewed",
        )
        parent = Path(observed["parent"])
        retained = Path(observed["retained"])
        sentinel = Path(observed["sentinel"])
        replacement_survived = sentinel.read_bytes()
        retained_payloads = _tree(retained)
    finally:
        for key in ("parent", "retained"):
            path = observed.get(key)
            if path is not None:
                shutil.rmtree(Path(path), ignore_errors=True)

    expected_status = "fallback" if backend_raises else "current"
    assert result["derivation_status"] == expected_status
    assert observed["consumed"] == original_payload
    assert replacement_survived == b"replacement must survive"
    assert retained_payloads == {}
    assert dict(os.environ) == environment_before
