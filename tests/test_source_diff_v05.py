from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
from typing import Any

from mem_core.errors import MemError
import pytest

from pco import sources as sources_module
from pco.proposals import materials as materials_module
from pco.proposals.materials import MAX_INPUT_MATERIAL_BYTES
from pco.sources import source_diff
from pco.workspace import Workspace

NOW = "2026-09-09T10:00:00+00:00"


def _source_record(
    source_id: str,
    locator: str,
    snapshot: bytes,
    *,
    revision: int = 1,
    status: str = "active",
    content_hash: str | None = None,
    snapshot_ref: str | None = None,
) -> dict[str, Any]:
    hex_digest = hashlib.sha256(snapshot).hexdigest()
    return {
        "id": source_id,
        "revision": revision,
        "recorded_at": NOW,
        "schema_version": "pco/source/v1",
        "commit_ref": "commit_source_diff_fixture",
        "payload": {
            "previous_revision": None if revision == 1 else revision - 1,
            "change_summary": "Create source" if revision == 1 else "Revise source",
            "body_markdown": "A source used by the immutable diff tests.",
            "locator": locator,
            "title": source_id,
            "retrieved_at": NOW,
            "provider_label": "test fixture",
            "content_hash": content_hash or f"sha256:{hex_digest}",
            "snapshot_ref": snapshot_ref or f"source_blobs/{hex_digest}",
            "fidelity": "registered_source_excerpt",
            "used_excerpts": ["fixture excerpt"],
            "status": status,
        },
    }


def _commit_sources(
    workspace: Workspace,
    records: list[dict[str, Any]],
    blobs: dict[str, bytes] | None = None,
) -> str:
    repository = workspace.repository
    stream = repository.profile.stream_path(repository.root, "sources")
    stream.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        ),
        encoding="utf-8",
        newline="\n",
    )
    for relative, content in (blobs or {}).items():
        target = repository.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    repository._git("add", ".")
    repository._git("commit", "--no-verify", "-m", "seed source diff fixture")
    return repository.head()


def _commit_one_local(
    workspace: Workspace,
    path: Path,
    snapshot: bytes = b"original\n",
    *,
    source_id: str = "src_local",
    locator: str | None = None,
) -> dict[str, Any]:
    record = _source_record(source_id, locator or path.as_uri(), snapshot)
    _commit_sources(
        workspace,
        [record],
        {record["payload"]["snapshot_ref"]: snapshot},
    )
    return record


def _code(error: pytest.ExceptionInfo[MemError]) -> str:
    return error.value.detail.code


def test_local_source_diff_uses_complete_normalized_snapshots(
    workspace: Workspace, tmp_path: Path
) -> None:
    path = tmp_path / "journal.txt"
    path.write_bytes(b"line one\nline two\n")
    _commit_one_local(workspace, path, b"line one\nline two\n")
    path.write_bytes(b"\xef\xbb\xbfline one\r\nchanged\r")

    result = source_diff(workspace, "src_local")

    expected_hash = "sha256:" + hashlib.sha256(b"line one\nchanged\n").hexdigest()
    assert result == {
        "ok": True,
        "sources": [
            {
                "source_id": "src_local",
                "current_content_hash": expected_hash,
                "diff": (
                    "--- src_local@previous\n"
                    "+++ src_local@current\n"
                    "@@ -1,2 +1,2 @@\n"
                    " line one\n"
                    "-line two\n"
                    "+changed\n"
                ),
            }
        ],
    }


def test_local_source_diff_always_reports_hash_when_content_is_unchanged(
    workspace: Workspace, tmp_path: Path
) -> None:
    path = tmp_path / "unchanged.txt"
    path.write_bytes(b"unchanged\n")
    record = _commit_one_local(workspace, path, b"unchanged\n")

    result = source_diff(workspace, "src_local")

    assert result["sources"] == [
        {
            "source_id": "src_local",
            "current_content_hash": record["payload"]["content_hash"],
            "diff": "",
        }
    ]


def test_remote_source_requires_host_native_refresh(workspace: Workspace) -> None:
    record = _source_record("src_remote", "https://example.test/article", b"cached\n")
    _commit_sources(workspace, [record])

    assert source_diff(workspace, "src_remote") == {
        "ok": True,
        "sources": [{"source_id": "src_remote", "refresh_required": True}],
    }


def test_remote_source_does_not_use_local_or_legacy_reader(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _source_record(
        "src_remote",
        "https://example.test/article",
        b"cached\n",
        content_hash="sha256:" + "0" * 64,
        snapshot_ref="source_blobs/not-a-content-address",
    )
    _commit_sources(workspace, [record])

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("remote source invoked a material reader")

    monkeypatch.setattr(sources_module, "read_regular_file", forbidden, raising=False)

    assert source_diff(workspace)["sources"] == [
        {"source_id": "src_remote", "refresh_required": True}
    ]


def test_profile_valid_malformed_remote_locator_still_requires_refresh(
    workspace: Workspace,
) -> None:
    record = _source_record("src_remote", "http://[bad", b"cached\n")
    workspace.profile.validate_record_schema("sources", record)
    _commit_sources(workspace, [record])

    assert source_diff(workspace, "src_remote") == {
        "ok": True,
        "sources": [{"source_id": "src_remote", "refresh_required": True}],
    }


def test_malformed_claimed_file_locator_is_a_structured_material_failure(
    workspace: Workspace,
) -> None:
    record = _source_record("src_local", "FILE://[bad", b"cached\n")
    _commit_sources(
        workspace,
        [record],
        {record["payload"]["snapshot_ref"]: b"cached\n"},
    )

    with pytest.raises(MemError) as error:
        source_diff(workspace, "src_local")

    assert error.value.detail.code == "INPUT_MATERIAL_UNSAFE"
    assert error.value.detail.phase == "proposal_material"
    assert error.value.detail.canonical_changed is False
    assert error.value.detail.retry_safe is False
    assert error.value.detail.recovery == [
        "Use a stable regular UTF-8 text file as input material"
    ]


def test_exact_source_id_filter_preserves_collection_shape(
    workspace: Workspace,
) -> None:
    first = _source_record("src_first", "https://first.test", b"first\n")
    second = _source_record("src_second", "https://second.test", b"second\n")
    _commit_sources(workspace, [first, second])

    assert source_diff(workspace, "src_second") == {
        "ok": True,
        "sources": [{"source_id": "src_second", "refresh_required": True}],
    }


def test_unknown_or_retired_exact_source_id_is_a_structured_failure(
    workspace: Workspace,
) -> None:
    retired = _source_record(
        "src_retired",
        "https://retired.test",
        b"retired\n",
        status="tombstone",
    )
    _commit_sources(workspace, [retired])

    for source_id in ("src_missing", "src_retired"):
        with pytest.raises(MemError) as error:
            source_diff(workspace, source_id)
        assert _code(error) == "SOURCE_NOT_FOUND"
        assert error.value.detail.record_id == source_id
        assert error.value.detail.canonical_changed is False
        assert error.value.detail.recovery == [
            "Run pco source diff without an ID to list current sources"
        ]


def test_head_execution_failure_is_a_structured_integrity_error(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_head() -> str:
        raise OSError("git executable unavailable")

    monkeypatch.setattr(workspace.repository, "head", failed_head)

    with pytest.raises(MemError) as error:
        source_diff(workspace)

    assert error.value.detail.code == "WORKSPACE_INTEGRITY_ERROR"
    assert error.value.detail.phase == "source_diff"
    assert error.value.detail.canonical_changed is False
    assert error.value.detail.retry_safe is False
    assert error.value.detail.recovery == [
        "Restore canonical memory from a trusted copy and retry source diff"
    ]


def test_nonzero_head_failure_remains_a_structured_git_error(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["git", "rev-parse", "HEAD"],
            returncode=128,
            stdout="",
            stderr="cannot resolve HEAD",
        )

    monkeypatch.setattr("mem_core.repository.subprocess.run", failed_run)

    with pytest.raises(MemError) as error:
        source_diff(workspace)

    assert error.value.detail.code == "GIT_FAILED"
    assert error.value.detail.phase == "git"
    assert error.value.detail.canonical_changed is False
    assert error.value.detail.retry_safe is True
    assert error.value.detail.recovery == [
        "Run mem git verify",
        "Resolve the reported Git repository problem",
    ]


@pytest.mark.parametrize("failure", ["oserror", "nonzero"])
def test_git_show_failures_are_structured_workspace_integrity_errors(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    record = _source_record("src_remote", "https://example.test", b"cached\n")
    _commit_sources(workspace, [record])
    canonical_head = workspace.repository.head()
    monkeypatch.setattr(workspace.repository, "head", lambda: canonical_head)

    def failed_run(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        if failure == "oserror":
            raise OSError("git executable unavailable")
        return subprocess.CompletedProcess(
            args=["git", "show"],
            returncode=128,
            stdout=b"",
            stderr=b"cannot read canonical object",
        )

    monkeypatch.setattr(sources_module.subprocess, "run", failed_run)

    with pytest.raises(MemError) as error:
        source_diff(workspace)

    assert error.value.detail.code == "WORKSPACE_INTEGRITY_ERROR"
    assert error.value.detail.phase == "source_diff"
    assert error.value.detail.canonical_changed is False
    assert error.value.detail.retry_safe is False
    assert error.value.detail.recovery == [
        "Restore canonical memory from a trusted copy and retry source diff"
    ]


def test_selection_uses_latest_revision_and_excludes_non_active_sources(
    workspace: Workspace,
) -> None:
    records = [
        _source_record("src_current", "https://old.test", b"old\n"),
        _source_record(
            "src_current",
            "https://current.test",
            b"current\n",
            revision=2,
        ),
        _source_record(
            "src_superseded",
            "https://superseded.test",
            b"superseded\n",
            status="superseded",
        ),
        _source_record(
            "src_tombstone",
            "https://tombstone.test",
            b"tombstone\n",
            status="tombstone",
        ),
    ]
    _commit_sources(workspace, records)

    assert source_diff(workspace)["sources"] == [
        {"source_id": "src_current", "refresh_required": True}
    ]


def test_all_sources_are_ordered_by_source_id_utf8_bytes(workspace: Workspace) -> None:
    records = [
        _source_record("src_é", "https://accent.test", b"accent\n"),
        _source_record("src_z", "https://zed.test", b"zed\n"),
        _source_record("src_a", "https://aye.test", b"aye\n"),
    ]
    _commit_sources(workspace, records)

    result = source_diff(workspace)

    assert [item["source_id"] for item in result["sources"]] == [
        "src_a",
        "src_z",
        "src_é",
    ]


def test_results_are_deeply_independent_between_calls(workspace: Workspace) -> None:
    record = _source_record("src_remote", "https://example.test", b"remote\n")
    _commit_sources(workspace, [record])
    first = source_diff(workspace)
    first["sources"][0]["source_id"] = "mutated"
    first["sources"].append({"source_id": "injected"})

    assert source_diff(workspace) == {
        "ok": True,
        "sources": [{"source_id": "src_remote", "refresh_required": True}],
    }


@pytest.mark.parametrize(
    "locator_factory",
    [
        lambda path: f"file://attacker.test{path.as_posix()}",
        lambda path: path.as_uri() + "?version=2",
        lambda path: path.as_uri() + "#fragment",
        lambda path: "file:relative.txt",
        lambda path: "file:///tmp/bad%escape",
    ],
)
def test_local_source_diff_reuses_strict_file_uri_parser(
    workspace: Workspace, tmp_path: Path, locator_factory: Any
) -> None:
    path = tmp_path / "material.txt"
    path.write_bytes(b"current\n")
    locator = locator_factory(path)
    _commit_one_local(workspace, path, b"previous\n", locator=locator)

    with pytest.raises(MemError) as error:
        source_diff(workspace, "src_local")

    assert _code(error) == "INPUT_MATERIAL_UNSAFE"


def test_file_scheme_is_recognized_case_insensitively(
    workspace: Workspace, tmp_path: Path
) -> None:
    path = tmp_path / "case.txt"
    path.write_bytes(b"changed\n")
    _commit_one_local(
        workspace,
        path,
        b"previous\n",
        locator=path.as_uri().replace("file:", "FILE:", 1),
    )

    result = source_diff(workspace, "src_local")["sources"][0]

    assert result["current_content_hash"] == (
        "sha256:" + hashlib.sha256(b"changed\n").hexdigest()
    )
    assert "-previous" in result["diff"]
    assert "+changed" in result["diff"]


@pytest.mark.parametrize("kind", ["symlink", "fifo", "socket"])
def test_source_diff_rejects_non_regular_local_material_without_following(
    workspace: Workspace, tmp_path: Path, kind: str
) -> None:
    path = tmp_path / "unsafe"
    opened_socket: socket.socket | None = None
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"secret\n")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        opened_socket = socket.socket(socket.AF_UNIX)
        opened_socket.bind(str(path))
    try:
        _commit_one_local(workspace, path, b"previous\n")

        with pytest.raises(MemError) as error:
            source_diff(workspace, "src_local")

        assert _code(error) == "INPUT_MATERIAL_UNSAFE"
    finally:
        if opened_socket is not None:
            opened_socket.close()


def test_source_diff_enforces_the_material_size_limit(
    workspace: Workspace, tmp_path: Path
) -> None:
    path = tmp_path / "oversized.txt"
    path.write_bytes(b"")
    os.truncate(path, MAX_INPUT_MATERIAL_BYTES + 1)
    _commit_one_local(workspace, path, b"previous\n")

    with pytest.raises(MemError) as error:
        source_diff(workspace, "src_local")

    assert _code(error) == "INPUT_MATERIAL_TOO_LARGE"


def test_source_diff_rejects_local_material_changed_during_single_read(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "racing.txt"
    path.write_bytes(b"current\n")
    _commit_one_local(workspace, path, b"previous\n")
    real_fstat = os.fstat
    calls = 0

    def changed_fstat(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        observed = real_fstat(descriptor)
        if calls == 2:
            values = list(observed)
            values[stat.ST_SIZE] += 1
            return os.stat_result(values)
        return observed

    monkeypatch.setattr(materials_module.os, "fstat", changed_fstat)

    with pytest.raises(MemError) as error:
        source_diff(workspace, "src_local")

    assert _code(error) == "INPUT_MATERIAL_UNSAFE"


def test_missing_snapshot_blob_is_an_integrity_error(
    workspace: Workspace, tmp_path: Path
) -> None:
    path = tmp_path / "current.txt"
    path.write_bytes(b"current\n")
    record = _source_record("src_local", path.as_uri(), b"missing\n")
    _commit_sources(workspace, [record])

    with pytest.raises(MemError) as error:
        source_diff(workspace, "src_local")

    assert _code(error) == "SOURCE_SNAPSHOT_INVALID"
    assert error.value.detail.recovery == [
        "Restore canonical memory from a trusted copy and retry source diff"
    ]


def test_snapshot_ref_must_agree_exactly_with_content_hash(
    workspace: Workspace, tmp_path: Path
) -> None:
    path = tmp_path / "current.txt"
    path.write_bytes(b"current\n")
    snapshot = b"previous\n"
    other_ref = "source_blobs/" + "f" * 64
    record = _source_record(
        "src_local", path.as_uri(), snapshot, snapshot_ref=other_ref
    )
    _commit_sources(workspace, [record], {other_ref: snapshot})

    with pytest.raises(MemError) as error:
        source_diff(workspace, "src_local")

    assert _code(error) == "SOURCE_SNAPSHOT_INVALID"


def test_hash_corrupt_snapshot_blob_is_an_integrity_error(
    workspace: Workspace, tmp_path: Path
) -> None:
    path = tmp_path / "current.txt"
    path.write_bytes(b"current\n")
    record = _source_record("src_local", path.as_uri(), b"expected\n")
    _commit_sources(
        workspace,
        [record],
        {record["payload"]["snapshot_ref"]: b"corrupt\n"},
    )

    with pytest.raises(MemError) as error:
        source_diff(workspace, "src_local")

    assert _code(error) == "SOURCE_SNAPSHOT_INVALID"


def test_non_utf8_snapshot_blob_is_an_integrity_error(
    workspace: Workspace, tmp_path: Path
) -> None:
    path = tmp_path / "current.txt"
    path.write_bytes(b"current\n")
    invalid = b"\xff\n"
    record = _source_record("src_local", path.as_uri(), invalid)
    _commit_sources(
        workspace,
        [record],
        {record["payload"]["snapshot_ref"]: invalid},
    )

    with pytest.raises(MemError) as error:
        source_diff(workspace, "src_local")

    assert _code(error) == "SOURCE_SNAPSHOT_INVALID"


def test_snapshot_and_source_records_are_read_from_head_not_dirty_worktree(
    workspace: Workspace, tmp_path: Path
) -> None:
    path = tmp_path / "current.txt"
    path.write_bytes(b"changed\n")
    record = _commit_one_local(workspace, path, b"committed previous\n")
    canonical_head = workspace.repository.head()
    stream = workspace.repository.profile.stream_path(
        workspace.repository.root, "sources"
    )
    dirty_record = deepcopy(record)
    dirty_record["id"] = "src_dirty"
    dirty_record["payload"]["locator"] = "https://dirty.test"
    stream.write_text(json.dumps(dirty_record) + "\n", encoding="utf-8")
    (workspace.repository.root / record["payload"]["snapshot_ref"]).write_bytes(
        b"dirty previous\n"
    )

    result = source_diff(workspace)

    assert workspace.repository.head() == canonical_head
    assert [item["source_id"] for item in result["sources"]] == ["src_local"]
    assert "-committed previous" in result["sources"][0]["diff"]
    assert "dirty previous" not in result["sources"][0]["diff"]
