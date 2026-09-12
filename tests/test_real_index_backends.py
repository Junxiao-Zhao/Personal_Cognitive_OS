from __future__ import annotations

import os
from pathlib import Path
import shutil

import pytest

from pco.derivation_manifest import verify_derivation_tree
from pco import retrieval
from pco.retrieval import build_retrieval_generation, search

pytestmark = pytest.mark.skipif(
    os.environ.get("PCO_RUN_MILVUS") != "1",
    reason="set PCO_RUN_MILVUS=1 to exercise local Tantivy and Milvus Lite",
)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_real_backends_build_one_verified_caller_target(
    committed_workspace, tmp_path: Path
) -> None:
    target = tmp_path / "retrieval"
    commit = committed_workspace.repository.head()

    manifest = build_retrieval_generation(
        committed_workspace.repository, target, commit
    )

    assert manifest.kind == "retrieval_index"
    assert (target / "milvus.db").exists()
    assert (target / "tantivy").is_dir()
    assert (
        verify_derivation_tree(target, kind="retrieval_index", expected_commit=commit)[
            "ok"
        ]
        is True
    )


def test_real_milvus_artifact_queries_from_disposable_probe_copy(
    committed_workspace, tmp_path: Path
) -> None:
    from pymilvus import MilvusClient

    target = tmp_path / "retrieval"
    build_retrieval_generation(
        committed_workspace.repository,
        target,
        committed_workspace.repository.head(),
    )
    original = _tree(target)
    probe = tmp_path / "probe.db"
    shutil.copytree(target / "milvus.db", probe)
    client = None
    with retrieval._local_backend_environment():
        try:
            client = MilvusClient(str(probe), dedicated=True)
            client.load_collection("memory")
            response = client.search(
                "memory",
                data=[[0.0] * 256],
                limit=3,
                output_fields=["key"],
            )
        finally:
            retrieval._close_milvus(client, probe)

    assert response and response[0]
    assert _tree(target) == original


def test_real_backend_search_is_read_only_and_restores_environment(
    committed_workspace, tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "retrieval"
    build_retrieval_generation(
        committed_workspace.repository,
        target,
        committed_workspace.repository.head(),
    )
    monkeypatch.setenv("NO_PROXY", "example.test")
    monkeypatch.delenv("no_proxy", raising=False)
    before = _tree(target)

    result = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=target,
        query="reviewed canonical",
        mode="current",
        limit=5,
    )

    assert result["derivation_status"] == "current", result.get("fallback_diagnostic")
    assert result["items"]
    assert _tree(target) == before
    assert os.environ["NO_PROXY"] == "example.test"
    assert "no_proxy" not in os.environ


def test_real_backend_corruption_uses_canonical_fallback_without_repair(
    committed_workspace, tmp_path: Path
) -> None:
    target = tmp_path / "retrieval"
    build_retrieval_generation(
        committed_workspace.repository,
        target,
        committed_workspace.repository.head(),
    )
    milvus_payload = next(
        path
        for path in (target / "milvus.db").rglob("*")
        if path.is_file() and path.name != ".pco-empty"
    )
    milvus_payload.write_bytes(b"corrupt")
    before = _tree(target)

    result = search(
        repo_root=committed_workspace.paths.canonical,
        indexes_root=target,
        query="reviewed canonical",
        mode="current",
        limit=5,
    )

    assert result["derivation_status"] == "fallback"
    assert result["items"]
    assert _tree(target) == before
