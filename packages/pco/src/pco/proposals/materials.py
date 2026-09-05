"""Bounded, immutable capture of proposal input material."""

from __future__ import annotations

import codecs
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Iterable
from urllib.parse import unquote, urlsplit

from mem_core.errors import MemError

from .models import InputMaterial, LocalFileMaterial, ProposalArtifactMaterial


MAX_INPUT_MATERIAL_BYTES = 16_777_216
_READ_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class CapturedFile:
    """The exact and normalized forms read through one stable file handle."""

    raw_bytes: bytes
    normalized_bytes: bytes
    file_hash: str
    content_hash: str
    identity: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class CapturedMaterial:
    """One manifest descriptor bound to immutable captured content."""

    material: InputMaterial
    raw_bytes: bytes
    normalized_bytes: bytes
    content_hash: str
    file_hash: str | None
    identity: tuple[int, int, int, int]

    @property
    def material_id(self) -> str:
        return self.material.material_id

    @property
    def kind(self) -> str:
        return self.material.kind


def digest(data: bytes) -> str:
    """Return the canonical lowercase SHA-256 identifier for bytes."""

    return "sha256:" + hashlib.sha256(data).hexdigest()


def _material_error(
    code: str,
    message: str,
    *,
    path: Path | None = None,
    value: object = None,
) -> MemError:
    recovery = (
        ["Reduce the material to a reviewed selected excerpt"]
        if code == "INPUT_MATERIAL_TOO_LARGE"
        else ["Use a stable regular UTF-8 text file as input material"]
    )
    return MemError(
        code,
        "proposal_material",
        message,
        path=None if path is None else str(path),
        value=value,
        recovery=recovery,
        canonical_changed=False,
        retry_safe=False,
    )


def _raise_too_large(limit: int, *, form: str) -> None:
    raise _material_error(
        "INPUT_MATERIAL_TOO_LARGE",
        f"Input material {form} bytes exceed the {limit}-byte limit",
        value={"form": form, "limit": limit},
    )


def _normalize_utf8(
    chunks: Iterable[bytes], *, limit: int, capture_raw: bool
) -> tuple[bytes | None, bytes]:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("limit must be a non-negative integer")

    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    raw = bytearray() if capture_raw else None
    normalized = bytearray()
    raw_size = 0
    pending_cr = False

    def extend_normalized(text: str) -> None:
        nonlocal pending_cr
        if "\x00" in text or "\ufeff" in text:
            raise _material_error(
                "INPUT_MATERIAL_INVALID",
                "Input material contains a NUL or non-leading byte-order mark",
            )

        if pending_cr:
            if text.startswith("\n"):
                text = text[1:]
            text = "\n" + text
            pending_cr = False
        if text.endswith("\r"):
            text = text[:-1]
            pending_cr = True
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        encoded = text.encode("utf-8")
        if len(normalized) + len(encoded) > limit:
            _raise_too_large(limit, form="normalized")
        normalized.extend(encoded)

    try:
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("material chunks must be bytes")
            if raw_size + len(chunk) > limit:
                _raise_too_large(limit, form="raw")
            raw_size += len(chunk)
            if raw is not None:
                raw.extend(chunk)
            decoded = decoder.decode(chunk, final=False)
            if decoded:
                extend_normalized(decoded)
        final_text = decoder.decode(b"", final=True)
        if final_text:
            extend_normalized(final_text)
    except UnicodeDecodeError as exc:
        raise _material_error(
            "INPUT_MATERIAL_INVALID",
            "Input material is not strict UTF-8 text",
            value={"start": exc.start, "reason": exc.reason},
        ) from exc

    if pending_cr:
        if len(normalized) + 1 > limit:
            _raise_too_large(limit, form="normalized")
        normalized.extend(b"\n")
    if normalized and not normalized.endswith(b"\n"):
        if len(normalized) + 1 > limit:
            _raise_too_large(limit, form="normalized")
        normalized.extend(b"\n")
    return (None if raw is None else bytes(raw)), bytes(normalized)


def normalize_utf8(chunks: Iterable[bytes], *, limit: int) -> bytes:
    """Normalize bounded byte chunks under the v0.5 material contract."""

    _, normalized = _normalize_utf8(chunks, limit=limit, capture_raw=False)
    return normalized


def _stat_identity(observed: os.stat_result) -> tuple[int, int, int, int]:
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
    )


def read_regular_file(path: Path, *, limit: int) -> CapturedFile:
    """Read one final-component no-follow regular file exactly once."""

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    no_follow = getattr(os, "O_NOFOLLOW", None)
    non_blocking = getattr(os, "O_NONBLOCK", None)
    if no_follow is None or non_blocking is None:
        raise _material_error(
            "INPUT_MATERIAL_UNSAFE",
            "This platform cannot safely open final-component input material",
            path=path,
        )

    flags = os.O_RDONLY | no_follow | non_blocking | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _material_error(
            "INPUT_MATERIAL_UNSAFE",
            "Input material cannot be opened without following links",
            path=path,
            value=str(exc),
        ) from exc

    try:
        try:
            before = os.fstat(descriptor)
        except OSError as exc:
            raise _material_error(
                "INPUT_MATERIAL_UNSAFE",
                "Input material identity cannot be inspected",
                path=path,
                value=str(exc),
            ) from exc
        if not stat.S_ISREG(before.st_mode):
            raise _material_error(
                "INPUT_MATERIAL_UNSAFE",
                "Input material must be a regular file",
                path=path,
            )
        if before.st_size < 0:
            raise _material_error(
                "INPUT_MATERIAL_UNSAFE",
                "Input material reported an invalid file size",
                path=path,
            )
        if before.st_size > limit:
            _raise_too_large(limit, form="raw")

        def chunks() -> Iterable[bytes]:
            consumed = 0
            while True:
                request_size = min(_READ_CHUNK_BYTES, max(1, limit - consumed + 1))
                try:
                    chunk = os.read(descriptor, request_size)
                except OSError as exc:
                    raise _material_error(
                        "INPUT_MATERIAL_UNSAFE",
                        "Input material could not be read safely",
                        path=path,
                        value=str(exc),
                    ) from exc
                if not chunk:
                    return
                consumed += len(chunk)
                yield chunk

        raw_bytes, normalized_bytes = _normalize_utf8(
            chunks(), limit=limit, capture_raw=True
        )
        assert raw_bytes is not None
        try:
            after = os.fstat(descriptor)
        except OSError as exc:
            raise _material_error(
                "INPUT_MATERIAL_UNSAFE",
                "Input material identity cannot be rechecked",
                path=path,
                value=str(exc),
            ) from exc
        before_identity = _stat_identity(before)
        if _stat_identity(after) != before_identity:
            raise _material_error(
                "INPUT_MATERIAL_UNSAFE",
                "Input material changed while it was being read",
                path=path,
                value={"before": before_identity, "after": _stat_identity(after)},
            )
        return CapturedFile(
            raw_bytes=raw_bytes,
            normalized_bytes=normalized_bytes,
            file_hash=digest(raw_bytes),
            content_hash=digest(normalized_bytes),
            identity=before_identity,
        )
    finally:
        os.close(descriptor)


def _local_file_path(locator: str) -> Path:
    parsed = urlsplit(locator)
    if (
        parsed.scheme.lower() != "file"
        or parsed.netloc not in {"", "localhost"}
        or parsed.query
        or parsed.fragment
    ):
        raise _material_error(
            "INPUT_MATERIAL_UNSAFE",
            "Local input material must use a local file URI",
            value=locator,
        )
    try:
        decoded = unquote(parsed.path, errors="strict")
    except UnicodeDecodeError as exc:
        raise _material_error(
            "INPUT_MATERIAL_UNSAFE",
            "Local input material URI is not valid UTF-8",
            value=locator,
        ) from exc
    if not decoded or "\x00" in decoded:
        raise _material_error(
            "INPUT_MATERIAL_UNSAFE",
            "Local input material URI must contain a safe path",
            value=locator,
        )
    path = Path(decoded)
    if not path.is_absolute():
        raise _material_error(
            "INPUT_MATERIAL_UNSAFE",
            "Local input material URI path must be absolute",
            value=locator,
        )
    return path


def _artifact_path(root: Path, relative: PurePosixPath) -> Path:
    parts = relative.parts
    if (
        relative.is_absolute()
        or len(parts) < 2
        or parts[0] != "inputs"
        or any(part in {"", ".", ".."} for part in parts)
        or "\\" in relative.as_posix()
    ):
        raise _material_error(
            "INPUT_MATERIAL_UNSAFE",
            "Proposal input material must be contained beneath inputs/",
            path=root / Path(*parts),
        )
    return root / Path(*parts)


def capture_material(
    material: InputMaterial,
    *,
    proposal_root: Path,
    limit: int = MAX_INPUT_MATERIAL_BYTES,
) -> CapturedMaterial:
    """Bind one input descriptor to an exact, bounded file capture."""

    if isinstance(material, LocalFileMaterial):
        path = _local_file_path(material.locator)
        captured = read_regular_file(path, limit=limit)
        file_hash = None
    elif isinstance(material, ProposalArtifactMaterial):
        path = _artifact_path(proposal_root, material.path)
        captured = read_regular_file(path, limit=limit)
        file_hash = captured.file_hash
    else:  # pragma: no cover - InputMaterial is a closed discriminated union
        raise TypeError("unsupported input material descriptor")

    return CapturedMaterial(
        material=material,
        raw_bytes=captured.raw_bytes,
        normalized_bytes=captured.normalized_bytes,
        content_hash=captured.content_hash,
        file_hash=file_hash,
        identity=captured.identity,
    )
