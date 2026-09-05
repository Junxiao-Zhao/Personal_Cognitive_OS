from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import socket
import stat
from typing import Iterator

from mem_core.errors import MemError
import pytest

from pco.proposals.materials import (
    MAX_INPUT_MATERIAL_BYTES,
    CapturedMaterial,
    capture_material,
    normalize_utf8,
    read_regular_file,
)
from pco.proposals.models import LocalFileMaterial, ProposalArtifactMaterial


def _code(exc: pytest.ExceptionInfo[MemError]) -> str:
    return exc.value.detail.code


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        (b"", b""),
        (b"a", b"a\n"),
        (b"a\n\n", b"a\n\n"),
        (b"\xef\xbb\xbfa\r\nb\rc", b"a\nb\nc\n"),
        ("e\u0301\n".encode(), "e\u0301\n".encode()),
        (b" a \t\n", b" a \t\n"),
    ],
)
def test_normalization_vectors(raw: bytes, normalized: bytes) -> None:
    assert normalize_utf8([raw], limit=1024) == normalized


@pytest.mark.parametrize("raw", [b"\xff", b"\x00", b"\xff\xfeA\x00", b"\xfe\xff\x00A"])
def test_invalid_material_is_rejected(raw: bytes) -> None:
    with pytest.raises(MemError) as exc:
        normalize_utf8([raw], limit=1024)
    assert _code(exc) == "INPUT_MATERIAL_INVALID"


@pytest.mark.parametrize(
    ("chunks", "normalized"),
    [
        ([b"\xef", b"\xbb", b"\xbf", b"x"], b"x\n"),
        ([b"\xf0", b"\x9f", b"\x98", b"\x80"], "😀\n".encode()),
        ([b"a\r", b"\nb\r", b"c"], b"a\nb\nc\n"),
    ],
)
def test_normalization_is_independent_of_chunk_boundaries(
    chunks: list[bytes], normalized: bytes
) -> None:
    assert normalize_utf8(chunks, limit=1024) == normalized


def test_empty_chunk_does_not_split_crlf_pair() -> None:
    assert normalize_utf8([b"a\r", b"", b"\nb"], limit=1024) == b"a\nb\n"


@pytest.mark.parametrize(
    "chunks",
    [
        [b"a", b"\xef\xbb\xbf", b"b"],
        [b"\xef\xbb\xbf", b"\xef\xbb\xbf"],
    ],
)
def test_utf8_bom_is_allowed_only_once_at_the_start(chunks: list[bytes]) -> None:
    with pytest.raises(MemError) as exc:
        normalize_utf8(chunks, limit=1024)
    assert _code(exc) == "INPUT_MATERIAL_INVALID"


def test_raw_limit_stops_before_requesting_more_input() -> None:
    consumed: list[int] = []

    def chunks() -> Iterator[bytes]:
        for index, chunk in enumerate((b"a", b"b", b"not-read")):
            consumed.append(index)
            yield chunk

    with pytest.raises(MemError) as exc:
        normalize_utf8(chunks(), limit=1)
    assert _code(exc) == "INPUT_MATERIAL_TOO_LARGE"
    assert consumed == [0, 1]


def test_normalized_limit_accounts_for_appended_line_feed() -> None:
    with pytest.raises(MemError) as exc:
        normalize_utf8([b"a"], limit=1)
    assert _code(exc) == "INPUT_MATERIAL_TOO_LARGE"


def test_limit_accepts_exact_raw_and_normalized_size() -> None:
    assert normalize_utf8([b"a\n"], limit=2) == b"a\n"


def test_default_material_limit_is_exactly_sixteen_mib() -> None:
    assert MAX_INPUT_MATERIAL_BYTES == 16_777_216


def _make_entry(path: Path, kind: str) -> socket.socket | None:
    if kind == "symlink":
        target = path.with_name("target")
        target.write_text("target", encoding="utf-8")
        path.symlink_to(target)
        return None
    if kind == "fifo":
        os.mkfifo(path)
        return None
    if kind == "socket":
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(path))
        return sock
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "socket"])
def test_material_reader_rejects_non_regular_file_without_blocking(
    tmp_path: Path, kind: str
) -> None:
    path = tmp_path / "input"
    sock = _make_entry(path, kind)
    try:
        with pytest.raises(MemError) as exc:
            read_regular_file(path, limit=16_777_216)
        assert _code(exc) == "INPUT_MATERIAL_UNSAFE"
    finally:
        if sock is not None:
            sock.close()


def test_reader_hashes_exact_and_normalized_bytes(tmp_path: Path) -> None:
    path = tmp_path / "input.txt"
    path.write_bytes(b"a\r\n")

    captured = read_regular_file(path, limit=1024)

    assert captured.raw_bytes == b"a\r\n"
    assert captured.normalized_bytes == b"a\n"
    assert captured.file_hash == "sha256:" + hashlib.sha256(b"a\r\n").hexdigest()
    assert captured.content_hash == "sha256:" + hashlib.sha256(b"a\n").hexdigest()
    assert captured.identity[:2] == (path.stat().st_dev, path.stat().st_ino)


def test_reader_opens_final_component_once_and_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "input.txt"
    path.write_bytes(b"content\n")
    real_open = os.open
    real_close = os.close
    opened: list[int] = []
    closed: list[int] = []

    def recording_open(*args: object, **kwargs: object) -> int:
        descriptor = real_open(*args, **kwargs)  # type: ignore[arg-type]
        opened.append(descriptor)
        return descriptor

    def recording_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "close", recording_close)

    read_regular_file(path, limit=1024)

    assert len(opened) == 1
    assert closed == opened


def test_reader_closes_descriptor_when_streaming_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "input.txt"
    path.write_bytes(b"ab")
    real_close = os.close
    closed: list[int] = []

    def recording_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(os, "close", recording_close)

    with pytest.raises(MemError) as exc:
        read_regular_file(path, limit=1)

    assert _code(exc) == "INPUT_MATERIAL_TOO_LARGE"
    assert len(closed) == 1


def test_reader_rejects_changed_identity_after_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "input.txt"
    path.write_bytes(b"content\n")
    real_fstat = os.fstat
    calls = 0

    def changed_fstat(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        observed = real_fstat(descriptor)
        if calls < 2:
            return observed
        values = list(observed)
        values[stat.ST_SIZE] += 1
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", changed_fstat)

    with pytest.raises(MemError) as exc:
        read_regular_file(path, limit=1024)

    assert _code(exc) == "INPUT_MATERIAL_UNSAFE"


def test_reader_stops_growing_stream_at_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "growing.txt"
    path.write_bytes(b"a")
    real_read = os.read
    reads = 0

    def growing_read(descriptor: int, count: int) -> bytes:
        nonlocal reads
        reads += 1
        if reads == 1:
            return real_read(descriptor, count)
        if reads == 2:
            return b"b"
        raise AssertionError("reader continued after limit + 1")

    monkeypatch.setattr(os, "read", growing_read)

    with pytest.raises(MemError) as exc:
        read_regular_file(path, limit=1)

    assert _code(exc) == "INPUT_MATERIAL_TOO_LARGE"
    assert reads == 2


def test_reader_rejects_platform_without_no_follow_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "input.txt"
    path.write_text("safe", encoding="utf-8")
    monkeypatch.delattr(os, "O_NOFOLLOW")

    with pytest.raises(MemError) as exc:
        read_regular_file(path, limit=1024)

    assert _code(exc) == "INPUT_MATERIAL_UNSAFE"


def test_reader_maps_no_follow_open_failure_to_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "input.txt"
    path.write_text("safe", encoding="utf-8")

    def failed_open(*args: object, **kwargs: object) -> int:
        raise OSError(errno.ELOOP, "symbolic link loop")

    monkeypatch.setattr(os, "open", failed_open)

    with pytest.raises(MemError) as exc:
        read_regular_file(path, limit=1024)

    assert _code(exc) == "INPUT_MATERIAL_UNSAFE"


def test_capture_local_file_preserves_locator_and_has_no_proposal_file_hash(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal entry.md"
    path.write_bytes(b"journal\r")
    material = LocalFileMaterial(
        material_id="journal",
        kind="local_file",
        locator=path.as_uri(),
        fidelity="full_source",
    )

    captured = capture_material(material, proposal_root=tmp_path)

    assert isinstance(captured, CapturedMaterial)
    assert captured.material is material
    assert captured.normalized_bytes == b"journal\n"
    assert captured.file_hash is None
    assert captured.content_hash == "sha256:" + hashlib.sha256(b"journal\n").hexdigest()


def test_capture_proposal_artifact_uses_contained_path_and_exact_file_hash(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "inputs" / "excerpt.txt"
    artifact.parent.mkdir()
    artifact.write_bytes(b"excerpt\r\n")
    material = ProposalArtifactMaterial(
        material_id="excerpt",
        kind="proposal_artifact",
        path="inputs/excerpt.txt",
        fidelity="selected_excerpt",
    )

    captured = capture_material(material, proposal_root=tmp_path)

    assert captured.material is material
    assert captured.normalized_bytes == b"excerpt\n"
    assert captured.file_hash == "sha256:" + hashlib.sha256(b"excerpt\r\n").hexdigest()


def test_capture_proposal_artifact_cannot_escape_root_even_if_model_is_tampered(
    tmp_path: Path,
) -> None:
    material = ProposalArtifactMaterial(
        material_id="excerpt",
        kind="proposal_artifact",
        path="inputs/excerpt.txt",
        fidelity="selected_excerpt",
    )
    object.__setattr__(material, "path", Path("../outside.txt"))

    with pytest.raises(MemError) as exc:
        capture_material(material, proposal_root=tmp_path)

    assert _code(exc) == "INPUT_MATERIAL_UNSAFE"


def test_capture_rejects_non_local_file_authority(tmp_path: Path) -> None:
    material = LocalFileMaterial(
        material_id="journal",
        kind="local_file",
        locator="file://remote.example/journal.md",
        fidelity="full_source",
    )

    with pytest.raises(MemError) as exc:
        capture_material(material, proposal_root=tmp_path)

    assert _code(exc) == "INPUT_MATERIAL_UNSAFE"
