"""Bounded, immutable capture of proposal input material."""

from __future__ import annotations

import codecs
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Callable, Iterable
from urllib.parse import unquote, urlsplit

from mem_core.errors import MemError

from .models import InputMaterial, LocalFileMaterial, ProposalArtifactMaterial


MAX_INPUT_MATERIAL_BYTES = 16_777_216
_READ_CHUNK_BYTES = 1024 * 1024
_UNSUPPORTED_LEADING_BOMS = (
    b"\x00\x00\xfe\xff",  # UTF-32 big endian
    b"\xff\xfe\x00\x00",  # UTF-32 little endian
    b"\xfe\xff",  # UTF-16 big endian
    b"\xff\xfe",  # UTF-16 little endian
)
_MALFORMED_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
NormalizedByteGuard = Callable[[int, int], None]


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


def normalize_utf8_stream(
    chunks: Iterable[bytes],
    *,
    limit: int,
    capture_raw: bool,
    normalized_byte_guard: NormalizedByteGuard | None = None,
) -> tuple[bytes | None, bytes]:
    """Capture and normalize a byte stream with incremental budget hooks.

    ``normalized_byte_guard`` is called with the retained normalized size and
    proposed addition before every normalized buffer extension. Callers may
    raise their own structured aggregate-limit error from the guard.
    """

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("limit must be a non-negative integer")

    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    raw = bytearray() if capture_raw else None
    normalized = bytearray()
    raw_size = 0
    pending_cr = False
    prefix = bytearray()
    decoder_started = False

    def extend_normalized(text: str) -> None:
        nonlocal pending_cr
        if "\x00" in text:
            raise _material_error(
                "INPUT_MATERIAL_INVALID",
                "Input material contains a NUL code point",
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
        if normalized_byte_guard is not None:
            normalized_byte_guard(len(normalized), len(encoded))
        normalized.extend(encoded)

    def reject_unsupported_bom(data: bytes) -> None:
        if any(data.startswith(bom) for bom in _UNSUPPORTED_LEADING_BOMS):
            raise _material_error(
                "INPUT_MATERIAL_INVALID",
                "Input material begins with a UTF-16 or UTF-32 byte-order mark",
            )

    def decode(data: bytes) -> None:
        decoded = decoder.decode(data, final=False)
        if decoded:
            extend_normalized(decoded)

    try:
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("material chunks must be bytes")
            if raw_size + len(chunk) > limit:
                _raise_too_large(limit, form="raw")
            raw_size += len(chunk)
            if raw is not None:
                raw.extend(chunk)
            if not decoder_started:
                prefix_bytes = min(4 - len(prefix), len(chunk))
                prefix.extend(chunk[:prefix_bytes])
                if len(prefix) < 4:
                    continue
                reject_unsupported_bom(bytes(prefix))
                decoder_started = True
                decode(bytes(prefix))
                prefix.clear()
                decode(chunk[prefix_bytes:])
            else:
                decode(chunk)
        if not decoder_started:
            reject_unsupported_bom(bytes(prefix))
            decode(bytes(prefix))
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
        if normalized_byte_guard is not None:
            normalized_byte_guard(len(normalized), 1)
        normalized.extend(b"\n")
    if normalized and not normalized.endswith(b"\n"):
        if len(normalized) + 1 > limit:
            _raise_too_large(limit, form="normalized")
        if normalized_byte_guard is not None:
            normalized_byte_guard(len(normalized), 1)
        normalized.extend(b"\n")
    return (None if raw is None else bytes(raw)), bytes(normalized)


def normalize_utf8(chunks: Iterable[bytes], *, limit: int) -> bytes:
    """Normalize bounded byte chunks under the v0.5 material contract."""

    _, normalized = normalize_utf8_stream(
        chunks, limit=limit, capture_raw=False
    )
    return normalized


def _stat_identity(observed: os.stat_result) -> tuple[int, int, int, int]:
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
    )


def _require_read_capabilities(*, anchored: bool, path: Path) -> None:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    non_blocking = getattr(os, "O_NONBLOCK", None)
    directory = getattr(os, "O_DIRECTORY", None)
    safe_metadata = os.stat in getattr(os, "supports_follow_symlinks", set())
    safe_anchoring = not anchored or (
        os.name == "posix"
        and directory is not None
        and os.open in getattr(os, "supports_dir_fd", set())
        and os.stat in getattr(os, "supports_dir_fd", set())
    )
    if (
        no_follow is None
        or non_blocking is None
        or not safe_metadata
        or not safe_anchoring
    ):
        raise _material_error(
            "INPUT_MATERIAL_UNSAFE",
            "This platform cannot safely inspect and open input material",
            path=path,
        )


def _named_stat(
    name: str | Path, *, parent_fd: int | None, display_path: Path
) -> os.stat_result:
    try:
        if parent_fd is None:
            return os.stat(name, follow_symlinks=False)
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise _material_error(
            "INPUT_MATERIAL_UNSAFE",
            "Input material named entry cannot be inspected safely",
            path=display_path,
            value=str(exc),
        ) from exc


def _read_regular_entry(
    name: str | Path,
    *,
    parent_fd: int | None,
    display_path: Path,
    limit: int,
    normalized_byte_guard: NormalizedByteGuard | None = None,
) -> CapturedFile:
    observed = _named_stat(name, parent_fd=parent_fd, display_path=display_path)
    if not stat.S_ISREG(observed.st_mode):
        raise _material_error(
            "INPUT_MATERIAL_UNSAFE",
            "Input material named entry must be a regular file",
            path=display_path,
        )

    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        if parent_fd is None:
            descriptor = os.open(name, flags)
        else:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise _material_error(
            "INPUT_MATERIAL_UNSAFE",
            "Input material cannot be opened without following links",
            path=display_path,
            value=str(exc),
        ) from exc

    try:
        try:
            before = os.fstat(descriptor)
        except OSError as exc:
            raise _material_error(
                "INPUT_MATERIAL_UNSAFE",
                "Input material identity cannot be inspected",
                path=display_path,
                value=str(exc),
            ) from exc
        if (
            not stat.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino) != (observed.st_dev, observed.st_ino)
        ):
            raise _material_error(
                "INPUT_MATERIAL_UNSAFE",
                "Input material changed between inspection and open",
                path=display_path,
            )
        if before.st_size < 0:
            raise _material_error(
                "INPUT_MATERIAL_UNSAFE",
                "Input material reported an invalid file size",
                path=display_path,
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
                        path=display_path,
                        value=str(exc),
                    ) from exc
                if not chunk:
                    return
                consumed += len(chunk)
                yield chunk

        raw_bytes, normalized_bytes = normalize_utf8_stream(
            chunks(),
            limit=limit,
            capture_raw=True,
            normalized_byte_guard=normalized_byte_guard,
        )
        assert raw_bytes is not None
        try:
            after = os.fstat(descriptor)
        except OSError as exc:
            raise _material_error(
                "INPUT_MATERIAL_UNSAFE",
                "Input material identity cannot be rechecked",
                path=display_path,
                value=str(exc),
            ) from exc
        before_identity = _stat_identity(before)
        if _stat_identity(after) != before_identity:
            raise _material_error(
                "INPUT_MATERIAL_UNSAFE",
                "Input material changed while it was being read",
                path=display_path,
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


def read_regular_file(
    path: Path,
    *,
    limit: int,
    normalized_byte_guard: NormalizedByteGuard | None = None,
) -> CapturedFile:
    """Read one named final-component no-follow regular file exactly once."""

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    _require_read_capabilities(anchored=False, path=path)
    return _read_regular_entry(
        path,
        parent_fd=None,
        display_path=path,
        limit=limit,
        normalized_byte_guard=normalized_byte_guard,
    )


def _local_file_path(locator: str) -> Path:
    parsed = urlsplit(locator)
    try:
        port = parsed.port
    except ValueError as exc:
        raise _material_error(
            "INPUT_MATERIAL_UNSAFE",
            "Local input material URI has an invalid authority",
            value=locator,
        ) from exc
    local_authority = (
        parsed.hostname in {None, "localhost"}
        and parsed.username is None
        and parsed.password is None
        and port is None
    )
    if (
        parsed.scheme.lower() != "file"
        or not local_authority
        or parsed.query
        or parsed.fragment
        or _MALFORMED_PERCENT_ESCAPE.search(parsed.path) is not None
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


def _artifact_parts(root: Path, relative: PurePosixPath) -> tuple[str, ...]:
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
    return parts


def _open_anchored_directory(
    name: str | Path,
    *,
    parent_fd: int | None,
    display_path: Path,
    observed: os.stat_result | None = None,
) -> int:
    try:
        if parent_fd is None:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
            )
        else:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_fd,
            )
    except OSError as exc:
        raise _material_error(
            "INPUT_MATERIAL_UNSAFE",
            "Proposal artifact traversal encountered an unsafe directory",
            path=display_path,
            value=str(exc),
        ) from exc
    try:
        try:
            anchored = os.fstat(descriptor)
        except OSError as exc:
            raise _material_error(
                "INPUT_MATERIAL_UNSAFE",
                "Proposal artifact directory identity cannot be inspected",
                path=display_path,
                value=str(exc),
            ) from exc
        if not stat.S_ISDIR(anchored.st_mode) or (
            observed is not None
            and (anchored.st_dev, anchored.st_ino)
            != (observed.st_dev, observed.st_ino)
        ):
            raise _material_error(
                "INPUT_MATERIAL_UNSAFE",
                "Proposal artifact directory identity changed while opening",
                path=display_path,
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _capture_proposal_artifact(
    material: ProposalArtifactMaterial,
    *,
    proposal_root: Path,
    limit: int,
    normalized_byte_guard: NormalizedByteGuard | None = None,
) -> CapturedFile:
    parts = _artifact_parts(proposal_root, material.path)
    _require_read_capabilities(anchored=True, path=proposal_root)
    root_observed = _named_stat(
        proposal_root,
        parent_fd=None,
        display_path=proposal_root,
    )
    if not stat.S_ISDIR(root_observed.st_mode):
        raise _material_error(
            "INPUT_MATERIAL_UNSAFE",
            "Proposal root must be a real directory",
            path=proposal_root,
        )

    anchors: list[int] = []
    try:
        anchors.append(
            _open_anchored_directory(
                proposal_root,
                parent_fd=None,
                display_path=proposal_root,
                observed=root_observed,
            )
        )
        display = proposal_root
        for part in parts[:-1]:
            display /= part
            anchors.append(
                _open_anchored_directory(
                    part,
                    parent_fd=anchors[-1],
                    display_path=display,
                )
            )
        return _read_regular_entry(
            parts[-1],
            parent_fd=anchors[-1],
            display_path=display / parts[-1],
            limit=limit,
            normalized_byte_guard=normalized_byte_guard,
        )
    finally:
        for descriptor in reversed(anchors):
            os.close(descriptor)


def capture_material(
    material: InputMaterial,
    *,
    proposal_root: Path,
    limit: int = MAX_INPUT_MATERIAL_BYTES,
    normalized_byte_guard: NormalizedByteGuard | None = None,
) -> CapturedMaterial:
    """Bind one input descriptor to an exact, bounded file capture."""

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    if isinstance(material, LocalFileMaterial):
        path = _local_file_path(material.locator)
        captured = read_regular_file(
            path,
            limit=limit,
            normalized_byte_guard=normalized_byte_guard,
        )
        file_hash = None
    elif isinstance(material, ProposalArtifactMaterial):
        captured = _capture_proposal_artifact(
            material,
            proposal_root=proposal_root,
            limit=limit,
            normalized_byte_guard=normalized_byte_guard,
        )
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
