"""Receipt-bound, non-canonical proposal archival and safe abort removal."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import stat
from typing import Any

from mem_core.errors import MemError

from .materials import digest
from .models import ProposalArtifactReceiptMaterial, ProposalCommitReceipt
from .snapshot import capture_snapshot_from_root


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    status: str
    active_path: str
    committed_path: str
    warning: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "archive_status": self.status,
            "archive_paths": {
                "active": self.active_path,
                "committed": self.committed_path,
            },
        }
        if self.warning is not None:
            result["archive_warning"] = dict(self.warning)
        return result


def _warning(status: str, active: Path, target: Path, message: str) -> ArchiveResult:
    actions = {
        "pending": f"Rerun pco proposal commit {active.name}",
        "missing": "No recovery is required; canonical memory is committed",
        "conflict": "Preserve both reported paths and resolve the review artifact manually",
    }
    return ArchiveResult(
        status,
        str(active),
        str(target),
        {
            "code": f"PROPOSAL_ARCHIVE_{status.upper()}",
            "message": message,
            "suggested_action": actions[status],
        },
    )


def _lstat_kind(path: Path) -> str:
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unsafe"
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    return "unsafe"


def _tree_matches_receipt(root: Path, receipt: ProposalCommitReceipt) -> bool:
    try:
        snapshot = capture_snapshot_from_root(
            root, include_local=False, expected_review_id=receipt.review_id
        )
    except (MemError, OSError, TypeError, ValueError):
        return False
    if snapshot.manifest.review_id != receipt.review_id:
        return False
    if digest(snapshot.manifest_bytes) != receipt.manifest_hash:
        return False
    files = {item.path.as_posix(): item.file_hash for item in snapshot.business_files}
    expected_files = {item.path.as_posix(): item.hash for item in receipt.files}
    if files != expected_files:
        return False
    artifacts = {
        item.material.path.as_posix(): (item.content_hash, item.file_hash)
        for item in snapshot.input_material
    }
    expected_artifacts = {
        item.path.as_posix(): (item.hash, item.file_hash)
        for item in receipt.input_material
        if isinstance(item, ProposalArtifactReceiptMaterial)
    }
    return artifacts == expected_artifacts


def _verified_tree_identity(
    root: Path, receipt: ProposalCommitReceipt
) -> tuple[int, int, int, int, int, int] | None:
    try:
        before = os.stat(root, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISDIR(before.st_mode) or not _tree_matches_receipt(root, receipt):
        return None
    try:
        after = os.stat(root, follow_symlinks=False)
    except OSError:
        return None
    return _identity(after) if _identity(after) == _identity(before) else None


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        stat.S_IFMT(info.st_mode),
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _open_directory(path: Path) -> tuple[int, tuple[int, int, int, int, int, int]]:
    observed = os.stat(path, follow_symlinks=False)
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
    )
    anchored = os.fstat(descriptor)
    if not stat.S_ISDIR(observed.st_mode) or _identity(observed)[:3] != _identity(anchored)[:3]:
        os.close(descriptor)
        raise OSError("directory identity changed while opening")
    return descriptor, _identity(anchored)


def atomic_rename(
    root_fd: int,
    active_name: str,
    committed_fd: int,
    target_name: str,
    expected_identity: tuple[int, int, int, int, int, int],
) -> None:
    """Rename one identity-checked child between descriptor-anchored parents."""

    current = os.stat(active_name, dir_fd=root_fd, follow_symlinks=False)
    if _identity(current) != expected_identity:
        raise FileExistsError("active proposal changed before archive rename")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-replace rename is unavailable on this platform",
        )
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(
        root_fd,
        os.fsencode(active_name),
        committed_fd,
        os.fsencode(target_name),
        1,  # RENAME_NOREPLACE
    ) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), target_name)


def _rename_exact_active(
    root: Path,
    active: Path,
    target: Path,
    expected_active_identity: tuple[int, int, int, int, int, int],
) -> None:
    root_fd, _root_identity = _open_directory(root)
    committed_fd: int | None = None
    try:
        try:
            os.mkdir(".committed", mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        committed_info = os.stat(".committed", dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(committed_info.st_mode):
            raise OSError("committed proposal root is unsafe")
        committed_fd = os.open(
            ".committed",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=root_fd,
        )
        if _identity(os.fstat(committed_fd))[:3] != _identity(committed_info)[:3]:
            raise OSError("committed proposal root changed while opening")
        active_info = os.stat(active.name, dir_fd=root_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(active_info.st_mode)
            or _identity(active_info) != expected_active_identity
        ):
            raise OSError("active proposal is no longer a directory")
        try:
            os.stat(target.name, dir_fd=committed_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("archive destination appeared before rename")
        atomic_rename(
            root_fd,
            active.name,
            committed_fd,
            target.name,
            _identity(active_info),
        )
    finally:
        if committed_fd is not None:
            os.close(committed_fd)
        os.close(root_fd)


def archive_committed_proposal(paths: Any, receipt_value: Any) -> ArchiveResult:
    """Attempt one post-commit archive without reading external input material."""

    receipt = (
        receipt_value
        if isinstance(receipt_value, ProposalCommitReceipt)
        else ProposalCommitReceipt.model_validate(receipt_value)
    )
    root = Path(paths.proposals)
    active = root / receipt.review_id
    committed_root = root / ".committed"
    target = committed_root / receipt.receipt_id
    committed_root_kind = _lstat_kind(committed_root)
    if committed_root_kind not in {"missing", "directory"}:
        return _warning(
            "conflict", active, target, "Committed proposal root is not a real directory"
        )
    active_kind = _lstat_kind(active)
    target_kind = _lstat_kind(target)
    if active_kind == "missing" and target_kind == "missing":
        return _warning("missing", active, target, "Neither review artifact path exists")
    if active_kind != "missing" and target_kind != "missing":
        return _warning("conflict", active, target, "Active and committed review paths both exist")
    if target_kind != "missing":
        if target_kind == "directory" and _verified_tree_identity(target, receipt) is not None:
            return ArchiveResult("archived", str(active), str(target))
        return _warning(
            "conflict",
            active,
            target,
            "Committed destination does not match its receipt",
        )
    active_identity = (
        _verified_tree_identity(active, receipt)
        if active_kind == "directory"
        else None
    )
    if active_identity is None:
        return _warning(
            "conflict",
            active,
            target,
            "Active proposal does not exactly match its receipt",
        )
    try:
        _rename_exact_active(root, active, target, active_identity)
    except FileExistsError:
        return _warning("conflict", active, target, "Proposal paths changed before archive rename")
    except OSError as exc:
        # A rename can complete even if acknowledgement is lost. Reclassify
        # from the receipt-bound paths before reporting a retryable warning.
        if (
            _lstat_kind(active) == "missing"
            and _lstat_kind(target) == "directory"
            and _verified_tree_identity(target, receipt) is not None
        ):
            return ArchiveResult("archived", str(active), str(target))
        if (
            _lstat_kind(active) == "directory"
            and _lstat_kind(target) == "missing"
            and _verified_tree_identity(active, receipt) is not None
        ):
            return _warning(
                "pending", active, target, f"Atomic archive rename failed: {exc}"
            )
        return _warning(
            "conflict", active, target, "Proposal paths changed during archive rename"
        )
    return ArchiveResult("archived", str(active), str(target))


def _remove_tree(directory_fd: int, display: Path) -> None:
    with os.scandir(directory_fd) as entries:
        children = sorted(entries, key=lambda item: item.name.encode("utf-8"))
    for child in children:
        path = display / child.name
        before = child.stat(follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            child_fd = os.open(
                child.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory_fd,
            )
            try:
                anchored = os.fstat(child_fd)
                if _identity(before)[:3] != _identity(anchored)[:3]:
                    raise OSError(f"directory changed while aborting: {path}")
                _remove_tree(child_fd, path)
                named = os.stat(child.name, dir_fd=directory_fd, follow_symlinks=False)
                if _identity(named)[:3] != _identity(anchored)[:3]:
                    raise OSError(f"directory changed before removal: {path}")
            finally:
                os.close(child_fd)
            os.rmdir(child.name, dir_fd=directory_fd)
        elif stat.S_ISREG(before.st_mode):
            file_fd = os.open(
                child.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory_fd,
            )
            try:
                anchored = os.fstat(file_fd)
                named = os.stat(child.name, dir_fd=directory_fd, follow_symlinks=False)
                if (
                    _identity(before)[:3] != _identity(anchored)[:3]
                    or _identity(named)[:3] != _identity(anchored)[:3]
                ):
                    raise OSError(f"file changed before removal: {path}")
                os.unlink(child.name, dir_fd=directory_fd)
            finally:
                os.close(file_fd)
        else:
            raise OSError(f"unsafe entry cannot be aborted: {path}")


def _preflight_safe_tree(directory_fd: int, display: Path) -> None:
    """Reject the complete tree before the first destructive abort operation."""

    with os.scandir(directory_fd) as entries:
        children = sorted(entries, key=lambda item: item.name.encode("utf-8"))
    for child in children:
        path = display / child.name
        info = child.stat(follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child_fd = os.open(
                child.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory_fd,
            )
            try:
                if _identity(info)[:3] != _identity(os.fstat(child_fd))[:3]:
                    raise OSError(f"directory changed during abort preflight: {path}")
                _preflight_safe_tree(child_fd, path)
            finally:
                os.close(child_fd)
        elif not stat.S_ISREG(info.st_mode):
            raise OSError(f"unsafe entry cannot be aborted: {path}")


def remove_active_proposal(paths: Any, review_id: str) -> None:
    root = Path(paths.proposals)
    root_fd, _ = _open_directory(root)
    active_fd: int | None = None
    try:
        observed = os.stat(review_id, dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(observed.st_mode):
            raise OSError("active proposal must be a real directory")
        active_fd = os.open(
            review_id,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=root_fd,
        )
        anchored = os.fstat(active_fd)
        if _identity(observed)[:3] != _identity(anchored)[:3]:
            raise OSError("active proposal changed while opening")
        _preflight_safe_tree(active_fd, root / review_id)
        _remove_tree(active_fd, root / review_id)
        named = os.stat(review_id, dir_fd=root_fd, follow_symlinks=False)
        if _identity(named)[:3] != _identity(anchored)[:3]:
            raise OSError("active proposal changed before removal")
        os.rmdir(review_id, dir_fd=root_fd)
    finally:
        if active_fd is not None:
            os.close(active_fd)
        os.close(root_fd)
