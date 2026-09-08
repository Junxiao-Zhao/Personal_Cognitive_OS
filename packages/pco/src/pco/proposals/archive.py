"""Receipt-bound, non-canonical proposal archival and safe abort removal."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import re
import stat
from typing import Any
import uuid

from mem_core.errors import MemError

from .materials import digest
from .models import ProposalArtifactReceiptMaterial, ProposalCommitReceipt
from .snapshot import (
    ProposalSnapshot,
    capture_snapshot_from_descriptor,
    capture_snapshot_from_root,
)


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


def _snapshot_matches_receipt(
    snapshot: ProposalSnapshot, receipt: ProposalCommitReceipt
) -> bool:
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


def _tree_matches_receipt(root: Path, receipt: ProposalCommitReceipt) -> bool:
    try:
        snapshot = capture_snapshot_from_root(
            root, include_local=False, expected_review_id=receipt.review_id
        )
    except (MemError, OSError, TypeError, ValueError):
        return False
    return _snapshot_matches_receipt(snapshot, receipt)


def _anchored_tree_matches_receipt(
    directory_fd: int, display: Path, receipt: ProposalCommitReceipt
) -> bool:
    try:
        snapshot = capture_snapshot_from_descriptor(
            directory_fd,
            display,
            include_local=False,
            expected_review_id=receipt.review_id,
        )
    except (MemError, OSError, TypeError, ValueError, UnicodeError):
        return False
    return _snapshot_matches_receipt(snapshot, receipt)


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
    if (
        not stat.S_ISDIR(observed.st_mode)
        or _identity(observed)[:3] != _identity(anchored)[:3]
    ):
        os.close(descriptor)
        raise OSError("directory identity changed while opening")
    return descriptor, _identity(anchored)


def _open_child_directory(
    parent_fd: int, name: str, display: Path
) -> tuple[int, tuple[int, int, int, int, int, int]]:
    observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(observed.st_mode):
        raise OSError(
            errno.ENOTDIR, "directory entry is not a real directory", str(display)
        )
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    anchored = os.fstat(descriptor)
    if _identity(observed)[:3] != _identity(anchored)[:3]:
        os.close(descriptor)
        raise OSError(
            errno.ESTALE, "directory identity changed while opening", str(display)
        )
    return descriptor, _identity(anchored)


def _recheck_child(
    parent_fd: int,
    name: str,
    expected: tuple[int, int, int, int, int, int],
    display: Path,
) -> None:
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if _identity(current)[:3] != expected[:3]:
        raise OSError(errno.ESTALE, "directory entry was replaced", str(display))


def atomic_rename(
    workspace_fd: int,
    proposals_name: str,
    root_fd: int,
    active_name: str,
    active_fd: int,
    committed_fd: int,
    target_name: str,
    workspace_root: Path,
    expected_root_identity: tuple[int, int, int, int, int, int],
    expected_active_identity: tuple[int, int, int, int, int, int],
    expected_committed_identity: tuple[int, int, int, int, int, int],
    receipt: ProposalCommitReceipt,
) -> None:
    """Publish one receipt-bound tree through identity-anchored parents.

    Ordinary directory rename cannot stop a writer that retained an already
    open descendant descriptor from changing bytes after the rename syscall.
    Stronger post-syscall immutability requires a copy/publication protocol.
    """

    active_display = workspace_root / proposals_name / active_name
    committed_display = workspace_root / proposals_name / ".committed"
    _recheck_child(
        workspace_fd,
        proposals_name,
        expected_root_identity,
        workspace_root / proposals_name,
    )
    _recheck_child(root_fd, active_name, expected_active_identity, active_display)
    _recheck_child(
        root_fd, ".committed", expected_committed_identity, committed_display
    )
    try:
        os.stat(target_name, dir_fd=committed_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError("archive destination appeared before rename")
    if not _anchored_tree_matches_receipt(active_fd, active_display, receipt):
        raise OSError(
            errno.ESTALE, "active proposal changed after receipt verification"
        )
    # These are the only path lookups between the complete receipt recheck and
    # renameat2. They bind the source/destination names to the verified inodes.
    _recheck_child(
        workspace_fd,
        proposals_name,
        expected_root_identity,
        workspace_root / proposals_name,
    )
    _recheck_child(root_fd, active_name, expected_active_identity, active_display)
    _recheck_child(
        root_fd, ".committed", expected_committed_identity, committed_display
    )
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
    if (
        renameat2(
            root_fd,
            os.fsencode(active_name),
            committed_fd,
            os.fsencode(target_name),
            1,  # RENAME_NOREPLACE
        )
        != 0
    ):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), target_name)
    _recheck_child(
        workspace_fd,
        proposals_name,
        expected_root_identity,
        workspace_root / proposals_name,
    )
    _recheck_child(
        root_fd, ".committed", expected_committed_identity, committed_display
    )
    try:
        os.stat(active_name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise OSError(errno.ESTALE, "active proposal name remains after archive")
    _recheck_child(
        committed_fd,
        target_name,
        expected_active_identity,
        committed_display / target_name,
    )


def _rename_exact_active(
    root: Path,
    active: Path,
    target: Path,
    expected_active_identity: tuple[int, int, int, int, int, int],
    receipt: ProposalCommitReceipt,
) -> None:
    workspace_root = root.parent
    workspace_fd, _workspace_identity = _open_directory(workspace_root)
    root_fd: int | None = None
    active_fd: int | None = None
    committed_fd: int | None = None
    try:
        root_fd, root_identity = _open_child_directory(workspace_fd, root.name, root)
        try:
            os.mkdir(".committed", mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        committed_fd, committed_identity = _open_child_directory(
            root_fd, ".committed", root / ".committed"
        )
        active_fd, active_identity = _open_child_directory(root_fd, active.name, active)
        if active_identity != expected_active_identity:
            raise OSError("active proposal is no longer a directory")
        atomic_rename(
            workspace_fd,
            root.name,
            root_fd,
            active.name,
            active_fd,
            committed_fd,
            target.name,
            workspace_root,
            root_identity,
            active_identity,
            committed_identity,
            receipt,
        )
    finally:
        if active_fd is not None:
            os.close(active_fd)
        if committed_fd is not None:
            os.close(committed_fd)
        if root_fd is not None:
            os.close(root_fd)
        os.close(workspace_fd)


def _archive_poststate(
    root: Path,
    active: Path,
    target: Path,
    receipt: ProposalCommitReceipt,
    expected_active_identity: tuple[int, int, int, int, int, int],
) -> str:
    """Classify an uncertain rename through one newly anchored named hierarchy."""

    workspace_fd: int | None = None
    root_fd: int | None = None
    committed_fd: int | None = None
    active_fd: int | None = None
    target_fd: int | None = None
    try:
        workspace_fd, _ = _open_directory(root.parent)
        root_fd, _ = _open_child_directory(workspace_fd, root.name, root)
        committed_fd, _ = _open_child_directory(
            root_fd, ".committed", root / ".committed"
        )
        try:
            active_fd, active_identity = _open_child_directory(
                root_fd, active.name, active
            )
        except FileNotFoundError:
            active_identity = None
        try:
            target_fd, target_identity = _open_child_directory(
                committed_fd, target.name, target
            )
        except FileNotFoundError:
            target_identity = None
        if (
            active_identity is None
            and target_fd is not None
            and target_identity is not None
            and target_identity[:3] == expected_active_identity[:3]
            and _anchored_tree_matches_receipt(target_fd, target, receipt)
        ):
            return "archived"
        if (
            target_identity is None
            and active_fd is not None
            and active_identity is not None
            and active_identity[:3] == expected_active_identity[:3]
            and _anchored_tree_matches_receipt(active_fd, active, receipt)
        ):
            return "pending"
        return "conflict"
    except (MemError, OSError, TypeError, ValueError, UnicodeError):
        return "conflict"
    finally:
        for descriptor in (target_fd, active_fd, committed_fd, root_fd, workspace_fd):
            if descriptor is not None:
                os.close(descriptor)


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
            "conflict",
            active,
            target,
            "Committed proposal root is not a real directory",
        )
    active_kind = _lstat_kind(active)
    target_kind = _lstat_kind(target)
    if active_kind == "missing" and target_kind == "missing":
        return _warning(
            "missing", active, target, "Neither review artifact path exists"
        )
    if active_kind != "missing" and target_kind != "missing":
        return _warning(
            "conflict", active, target, "Active and committed review paths both exist"
        )
    if target_kind != "missing":
        if (
            target_kind == "directory"
            and _verified_tree_identity(target, receipt) is not None
        ):
            return ArchiveResult("archived", str(active), str(target))
        return _warning(
            "conflict",
            active,
            target,
            "Committed destination does not match its receipt",
        )
    active_identity = (
        _verified_tree_identity(active, receipt) if active_kind == "directory" else None
    )
    if active_identity is None:
        return _warning(
            "conflict",
            active,
            target,
            "Active proposal does not exactly match its receipt",
        )
    try:
        _rename_exact_active(root, active, target, active_identity, receipt)
    except FileExistsError:
        return _warning(
            "conflict", active, target, "Proposal paths changed before archive rename"
        )
    except OSError as exc:
        # A rename can complete even if acknowledgement is lost. Reclassify
        # through a fresh no-follow hierarchy and require the renamed inode.
        poststate = _archive_poststate(root, active, target, receipt, active_identity)
        if poststate == "archived":
            return ArchiveResult("archived", str(active), str(target))
        if exc.errno == errno.ESTALE:
            return _warning(
                "conflict",
                active,
                target,
                "Proposal parent or tree identity changed during archive",
            )
        if poststate == "pending":
            return _warning(
                "pending", active, target, f"Atomic archive rename failed: {exc}"
            )
        return _warning(
            "conflict", active, target, "Proposal paths changed during archive rename"
        )
    return ArchiveResult("archived", str(active), str(target))


def _remove_tree(directory_fd: int, display: Path) -> None:
    with os.scandir(directory_fd) as entries:
        children = sorted(entries, key=lambda item: os.fsencode(item.name))
    for child in children:
        try:
            child.name.encode("utf-8")
        except UnicodeError as exc:
            raise OSError(errno.EILSEQ, "proposal entry name is not UTF-8") from exc
        path = display / child.name
        before = child.stat(follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            child_fd = os.open(
                child.name,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
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
                os.O_RDONLY
                | os.O_NOFOLLOW
                | os.O_NONBLOCK
                | getattr(os, "O_CLOEXEC", 0),
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
        children = sorted(entries, key=lambda item: os.fsencode(item.name))
    for child in children:
        try:
            child.name.encode("utf-8")
        except UnicodeError as exc:
            raise OSError(errno.EILSEQ, "proposal entry name is not UTF-8") from exc
        path = display / child.name
        info = child.stat(follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child_fd = os.open(
                child.name,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
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


_REVIEW_ID = re.compile(r"^review_[A-Za-z0-9._-]+$")


def _abort_discovery_error(root: Path, review_id: str, names: list[str]) -> MemError:
    return MemError(
        "PROPOSAL_MANIFEST_MISMATCH",
        "proposal_abort",
        "The requested review is not the single active proposal",
        path=str(root / review_id),
        value=names,
        recovery=["Keep exactly one matching active review directory"],
        canonical_changed=False,
        retry_safe=False,
    )


def _detach_active_noreplace(
    workspace_fd: int,
    proposals_fd: int,
    pco_fd: int,
    tmp_fd: int,
    quarantine_fd: int,
    *,
    workspace_root: Path,
    proposals_identity: tuple[int, int, int, int, int, int],
    pco_identity: tuple[int, int, int, int, int, int],
    tmp_identity: tuple[int, int, int, int, int, int],
    quarantine_identity: tuple[int, int, int, int, int, int],
    active_name: str,
    active_fd: int,
    active_identity: tuple[int, int, int, int, int, int],
    tombstone_name: str,
) -> None:
    """Detach the exact active tree before any recursive unlink."""

    _recheck_child(
        workspace_fd,
        "PCO_PROPOSALS",
        proposals_identity,
        workspace_root / "PCO_PROPOSALS",
    )
    _recheck_child(workspace_fd, ".pco", pco_identity, workspace_root / ".pco")
    _recheck_child(pco_fd, "tmp", tmp_identity, workspace_root / ".pco/tmp")
    _recheck_child(
        tmp_fd,
        "proposal-abort-quarantine",
        quarantine_identity,
        workspace_root / ".pco/tmp/proposal-abort-quarantine",
    )
    _recheck_child(
        proposals_fd,
        active_name,
        active_identity,
        workspace_root / "PCO_PROPOSALS" / active_name,
    )
    # A second full preflight closes the discovery/preflight race before detach.
    _preflight_safe_tree(active_fd, workspace_root / "PCO_PROPOSALS" / active_name)
    _recheck_child(
        workspace_fd,
        "PCO_PROPOSALS",
        proposals_identity,
        workspace_root / "PCO_PROPOSALS",
    )
    _recheck_child(
        proposals_fd,
        active_name,
        active_identity,
        workspace_root / "PCO_PROPOSALS" / active_name,
    )
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOTSUP, "atomic no-replace detach is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            proposals_fd,
            os.fsencode(active_name),
            quarantine_fd,
            os.fsencode(tombstone_name),
            1,
        )
        != 0
    ):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), tombstone_name)
    try:
        os.stat(active_name, dir_fd=proposals_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise OSError(errno.ESTALE, "active proposal name remains after detach")
    _recheck_child(
        quarantine_fd,
        tombstone_name,
        active_identity,
        workspace_root / ".pco/tmp/proposal-abort-quarantine" / tombstone_name,
    )


def remove_active_proposal(paths: Any, review_id: str) -> dict[str, Any]:
    """Atomically remove one active name, then clean only its quarantine inode."""

    workspace_root = Path(paths.root).absolute()
    root = Path(paths.proposals).absolute()
    tmp = Path(paths.tmp).absolute()
    if (
        root != workspace_root / "PCO_PROPOSALS"
        or tmp != workspace_root / ".pco/tmp"
        or _REVIEW_ID.fullmatch(review_id) is None
    ):
        raise OSError(errno.EINVAL, "abort paths or review ID are invalid")
    workspace_fd, _workspace_identity = _open_directory(workspace_root)
    root_fd: int | None = None
    pco_fd: int | None = None
    tmp_fd: int | None = None
    quarantine_fd: int | None = None
    active_fd: int | None = None
    tombstone_name = f"{review_id}-{uuid.uuid4().hex}"
    quarantine_path = tmp / "proposal-abort-quarantine" / tombstone_name
    try:
        root_fd, root_identity = _open_child_directory(
            workspace_fd, "PCO_PROPOSALS", root
        )
        pco_fd, pco_identity = _open_child_directory(
            workspace_fd, ".pco", workspace_root / ".pco"
        )
        tmp_fd, tmp_identity = _open_child_directory(pco_fd, "tmp", tmp)
        try:
            os.mkdir("proposal-abort-quarantine", mode=0o700, dir_fd=tmp_fd)
        except FileExistsError:
            pass
        quarantine_fd, quarantine_identity = _open_child_directory(
            tmp_fd,
            "proposal-abort-quarantine",
            tmp / "proposal-abort-quarantine",
        )
        if os.fstat(root_fd).st_dev != os.fstat(quarantine_fd).st_dev:
            raise OSError(
                errno.EXDEV, "abort quarantine is not on the proposal filesystem"
            )

        names: list[str] = []
        with os.scandir(root_fd) as entries:
            children = sorted(entries, key=lambda entry: os.fsencode(entry.name))
        for child in children:
            try:
                child.name.encode("utf-8")
            except UnicodeError as exc:
                raise OSError(errno.EILSEQ, "proposal entry name is not UTF-8") from exc
            info = child.stat(follow_symlinks=False)
            if child.name == ".committed":
                if not stat.S_ISDIR(info.st_mode):
                    raise OSError(errno.ENOTDIR, "committed proposal root is unsafe")
                continue
            if (
                not stat.S_ISDIR(info.st_mode)
                or _REVIEW_ID.fullmatch(child.name) is None
            ):
                raise OSError(errno.EPERM, "active proposal entry is unsafe")
            names.append(child.name)
        if names != [review_id]:
            raise _abort_discovery_error(root, review_id, names)

        active_fd, active_identity = _open_child_directory(
            root_fd, review_id, root / review_id
        )
        _preflight_safe_tree(active_fd, root / review_id)
        _detach_active_noreplace(
            workspace_fd,
            root_fd,
            pco_fd,
            tmp_fd,
            quarantine_fd,
            workspace_root=workspace_root,
            proposals_identity=root_identity,
            pco_identity=pco_identity,
            tmp_identity=tmp_identity,
            quarantine_identity=quarantine_identity,
            active_name=review_id,
            active_fd=active_fd,
            active_identity=active_identity,
            tombstone_name=tombstone_name,
        )
        try:
            _remove_tree(active_fd, quarantine_path)
            _recheck_child(
                quarantine_fd,
                tombstone_name,
                active_identity,
                quarantine_path,
            )
            os.rmdir(tombstone_name, dir_fd=quarantine_fd)
        except (OSError, TypeError, ValueError, UnicodeError) as exc:
            return {
                "ok": True,
                "status": "aborted",
                "review_id": review_id,
                "cleanup_warning": {
                    "code": "PROPOSAL_ABORT_CLEANUP_PENDING",
                    "message": f"Detached proposal cleanup failed independently: {exc}",
                    "recovery_path": str(quarantine_path),
                    "suggested_action": "Inspect and remove only the reported quarantine remnant",
                },
            }
        return {"ok": True, "status": "aborted", "review_id": review_id}
    finally:
        for descriptor in (
            active_fd,
            quarantine_fd,
            tmp_fd,
            pco_fd,
            root_fd,
            workspace_fd,
        ):
            if descriptor is not None:
                os.close(descriptor)
