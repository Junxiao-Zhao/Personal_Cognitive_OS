"""Build, verify, and publish both disposable PCO derivations."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import stat
import tempfile
from typing import TYPE_CHECKING, Any, Iterator
import uuid

from mem_core.errors import MemError

from .derivation_manifest import derivation_error, verify_derivation_tree
from .projections import build_projection
from .retrieval import build_retrieval_generation
from .verify import verify_projection

if TYPE_CHECKING:
    from .workspace import Workspace


_DIRECTORY_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
)
_CLEANUP_MAX_PASSES = 16
_RENAME_NOREPLACE = 1


@dataclass(frozen=True)
class _TargetState:
    identity: tuple[int, int] | None


@dataclass
class _ProcessRoot:
    tmp_path: Path
    tmp_fd: int
    tmp_identity: tuple[int, int]
    name: str
    root_fd: int
    root_identity: tuple[int, int]
    preserve_on_cleanup: bool = False

    @property
    def anchored_path(self) -> Path:
        # Keep the final real directory component after the proc-fd link so the
        # Task 1/2 builders can independently lstat and anchor their parent.
        return Path(f"/proc/self/fd/{self.tmp_fd}") / self.name

    def require_bound(self) -> None:
        if _identity(os.fstat(self.tmp_fd)) != self.tmp_identity:
            raise derivation_error("Temporary parent descriptor identity changed")
        if _identity(os.fstat(self.root_fd)) != self.root_identity:
            raise derivation_error("Owned derivation root descriptor identity changed")
        _require_public_directory(self.tmp_path, self.tmp_identity)
        _require_named_directory(
            self.tmp_fd,
            self.name,
            self.root_identity,
            self.tmp_path / self.name,
        )

    def child_identity(self, name: str) -> tuple[int, int]:
        self.require_bound()
        try:
            observed = os.stat(name, dir_fd=self.root_fd, follow_symlinks=False)
        except OSError as exc:
            raise derivation_error(
                "Completed temporary output cannot be inspected",
                path=self.tmp_path / self.name / name,
                value=str(exc),
            ) from exc
        if not stat.S_ISDIR(observed.st_mode):
            raise derivation_error(
                "Completed temporary output must be a real directory",
                path=self.tmp_path / self.name / name,
            )
        return _identity(observed)


def verify_generation(path: Path, expected_commit: str | None = None) -> dict[str, Any]:
    """Verify one fixed whole-tree retrieval generation without writes."""

    return verify_derivation_tree(
        Path(path), kind="retrieval_index", expected_commit=expected_commit
    )


def read_head_before_publish(workspace: "Workspace") -> str:
    """Read canonical HEAD at the destructive publication boundary."""

    return workspace.repository.head()


def derive_all(
    workspace: "Workspace", *, discard_modified_projection: bool = False
) -> dict[str, Any]:
    """Rebuild both disposable outputs from one captured canonical HEAD."""

    try:
        source_commit = workspace.repository.head()
        initial_projection, initial_conflict = _inspect_projection(
            workspace.paths.projection, source_commit
        )
        if initial_conflict and not discard_modified_projection:
            raise _projection_modified(initial_conflict)
        initial_indexes = _inspect_real_root(workspace.paths.indexes)

        with _owned_process_root(workspace.paths.tmp) as process:
            projection_tmp = process.anchored_path / "PCO_MEMORY"
            indexes_tmp = process.anchored_path / "indexes"
            build_projection(workspace.repository, projection_tmp, source_commit)
            process.require_bound()
            build_retrieval_generation(workspace.repository, indexes_tmp, source_commit)
            process.require_bound()

            projection_result = verify_projection(
                projection_tmp, expected_commit=source_commit
            )
            if projection_result.get("ok") is not True:
                raise derivation_error(
                    "Completed projection did not pass verification",
                    path=projection_tmp,
                    value=projection_result,
                )
            projection_identity = process.child_identity("PCO_MEMORY")

            indexes_result = verify_generation(
                indexes_tmp, expected_commit=source_commit
            )
            if indexes_result.get("ok") is not True:
                raise derivation_error(
                    "Completed retrieval generation did not pass verification",
                    path=indexes_tmp,
                    value=indexes_result,
                )
            indexes_identity = process.child_identity("indexes")

            final_projection, final_conflict = _inspect_projection(
                workspace.paths.projection, source_commit
            )
            if final_conflict and not discard_modified_projection:
                raise _projection_modified(final_conflict)
            # A target changing between the initial preservation check and the
            # final publication check is not silently re-authorized.
            if initial_projection.identity != final_projection.identity:
                raise derivation_error(
                    "Projection root identity changed during derivation",
                    path=workspace.paths.projection,
                )
            indexes_state = _inspect_real_root(workspace.paths.indexes)
            if initial_indexes.identity != indexes_state.identity:
                raise derivation_error(
                    "Retrieval index root identity changed during derivation",
                    path=workspace.paths.indexes,
                )

            _publish_output(
                workspace,
                process,
                source_name="PCO_MEMORY",
                source_identity=projection_identity,
                target=workspace.paths.projection,
                target_state=final_projection,
                source_commit=source_commit,
                label="projection",
            )
            _publish_output(
                workspace,
                process,
                source_name="indexes",
                source_identity=indexes_identity,
                target=workspace.paths.indexes,
                target_state=indexes_state,
                source_commit=source_commit,
                label="retrieval index",
            )

            result: dict[str, Any] = {
                "ok": True,
                "source_commit": source_commit,
                "projection": "current",
                "index": "current",
            }
            conflict = final_conflict or initial_conflict
            if conflict:
                result.update(
                    {
                        "discarded_projection": conflict,
                        "discarded_modified": list(conflict["modified"]),
                        "discarded_missing": list(conflict["missing"]),
                        "discarded_additional": list(conflict["additional"]),
                    }
                )
            return result
    except MemError:
        raise
    except Exception as exc:
        raise derivation_error(
            "Disposable derivation could not be completed safely", value=str(exc)
        ) from exc


def _projection_modified(conflict: dict[str, list[str]]) -> MemError:
    return MemError(
        "PROJECTION_MODIFIED",
        "derive",
        "PCO_MEMORY contains changes that require explicit discard",
        canonical_changed=False,
        retry_safe=False,
        path="PCO_MEMORY",
        value={name: list(conflict[name]) for name in _CONFLICT_FIELDS},
        recovery=[
            "Run pco verify --path PCO_MEMORY and preserve intended changes in a proposal",
            "Or run pco derive all --discard-modified-projection after reviewing the conflict",
        ],
    )


_CONFLICT_FIELDS = ("modified", "missing", "additional")


def _inspect_projection(
    path: Path, expected_commit: str
) -> tuple[_TargetState, dict[str, list[str]] | None]:
    before = _inspect_real_root(path)
    result = verify_projection(path, expected_commit=expected_commit)
    after = _inspect_real_root(path)
    if before != after:
        raise derivation_error(
            "Projection root identity changed during inspection", path=path
        )
    conflict = {name: list(result.get(name, [])) for name in _CONFLICT_FIELDS}
    initializer_missing = (
        result.get("status") == "missing"
        and result.get("source_commit") is None
        and conflict
        == {
            "modified": [],
            "missing": ["manifest.json"],
            "additional": [],
        }
    )
    if initializer_missing or not any(conflict.values()):
        return after, None
    return after, conflict


def _inspect_real_root(path: Path) -> _TargetState:
    target = Path(path).absolute()
    try:
        observed = os.lstat(target)
    except FileNotFoundError:
        return _TargetState(None)
    except (OSError, TypeError, ValueError) as exc:
        raise derivation_error(
            "Disposable output root cannot be inspected safely",
            path=target,
            value=str(exc),
            retry_safe=False,
        ) from exc
    if not stat.S_ISDIR(observed.st_mode):
        raise derivation_error(
            "Disposable output root must be absent or a real directory",
            path=target,
            retry_safe=False,
        )
    return _TargetState(_identity(observed))


@contextmanager
def _owned_process_root(tmp_value: Path) -> Iterator[_ProcessRoot]:
    tmp_path = Path(tmp_value).absolute()
    try:
        observed = os.lstat(tmp_path)
        if not stat.S_ISDIR(observed.st_mode):
            raise derivation_error(
                "Workspace temporary root must be a real directory",
                path=tmp_path,
                retry_safe=False,
            )
        tmp_identity = _identity(observed)
        tmp_fd = os.open(tmp_path, _DIRECTORY_FLAGS)
    except MemError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise derivation_error(
            "Workspace temporary root cannot be opened safely",
            path=tmp_path,
            value=str(exc),
            retry_safe=False,
        ) from exc

    root_fd: int | None = None
    root_name: str | None = None
    root_identity: tuple[int, int] | None = None
    body_failed = False
    cleanup_error: BaseException | None = None
    process: _ProcessRoot | None = None
    cleanup_name: str | None = None
    try:
        if _identity(os.fstat(tmp_fd)) != tmp_identity:
            raise derivation_error(
                "Workspace temporary root identity changed while opening",
                path=tmp_path,
            )
        _require_public_directory(tmp_path, tmp_identity)
        anchored_parent = Path(f"/proc/self/fd/{tmp_fd}")
        created = Path(tempfile.mkdtemp(prefix="derive-", dir=anchored_parent))
        if created.parent != anchored_parent:
            raise derivation_error(
                "Temporary allocator returned a root outside the retained parent",
                path=created,
                retry_safe=False,
            )
        root_name = created.name
        if not root_name.startswith("derive-") or not _safe_name(root_name):
            raise derivation_error(
                "Temporary allocator returned an unsafe root name",
                path=created,
                retry_safe=False,
            )
        root_observed = os.stat(root_name, dir_fd=tmp_fd, follow_symlinks=False)
        if not stat.S_ISDIR(root_observed.st_mode):
            raise derivation_error(
                "Allocated derivation root is not a real directory", path=created
            )
        root_identity = _identity(root_observed)
        root_fd = os.open(root_name, _DIRECTORY_FLAGS, dir_fd=tmp_fd)
        process = _ProcessRoot(
            tmp_path,
            tmp_fd,
            tmp_identity,
            root_name,
            root_fd,
            root_identity,
        )
        process.require_bound()
        try:
            yield process
        except BaseException:
            body_failed = True
            raise
    finally:
        cleanup_allowed = process is None or not process.preserve_on_cleanup
        detached = False
        if cleanup_allowed and root_name is not None and root_identity is not None:
            cleanup_name = f".{root_name}.cleanup-{uuid.uuid4().hex}"
            try:
                _detach_directory_noreplace(
                    tmp_fd,
                    root_name,
                    tmp_fd,
                    cleanup_name,
                    root_identity,
                    source_display=tmp_path / root_name,
                    detached_display=tmp_path / cleanup_name,
                )
                detached = True
            except (OSError, TypeError, ValueError, MemError) as exc:
                cleanup_error = exc
        if cleanup_allowed and root_fd is not None:
            try:
                _erase_owned_directory(root_fd)
            except (OSError, TypeError, ValueError) as exc:
                cleanup_error = cleanup_error or exc
        if detached and cleanup_error is None and cleanup_name is not None:
            try:
                _require_named_directory(
                    tmp_fd,
                    cleanup_name,
                    root_identity,
                    tmp_path / cleanup_name,
                )
                os.rmdir(cleanup_name, dir_fd=tmp_fd)
            except (OSError, TypeError, ValueError, MemError) as exc:
                cleanup_error = exc
        if root_fd is not None:
            os.close(root_fd)
        os.close(tmp_fd)
        if cleanup_error is not None and not body_failed:
            raise derivation_error(
                "Owned derivation root could not be cleaned safely",
                value=str(cleanup_error),
            ) from cleanup_error


def _publish_output(
    workspace: "Workspace",
    process: _ProcessRoot,
    *,
    source_name: str,
    source_identity: tuple[int, int],
    target: Path,
    target_state: _TargetState,
    source_commit: str,
    label: str,
) -> None:
    target = Path(target).absolute()
    parent = target.parent
    parent_fd: int | None = None
    target_fd: int | None = None
    try:
        parent_observed = os.lstat(parent)
        if not stat.S_ISDIR(parent_observed.st_mode):
            raise derivation_error(
                "Publication parent must be a real directory", path=parent
            )
        parent_identity = _identity(parent_observed)
        parent_fd = os.open(parent, _DIRECTORY_FLAGS)
        if _identity(os.fstat(parent_fd)) != parent_identity:
            raise derivation_error("Publication parent identity changed", path=parent)
        _require_public_directory(parent, parent_identity)
        process.require_bound()
        _require_named_directory(
            process.root_fd,
            source_name,
            source_identity,
            process.tmp_path / process.name / source_name,
        )

        if target_state.identity is None:
            _require_name_absent(parent_fd, target.name, target)
        else:
            _require_named_directory(
                parent_fd, target.name, target_state.identity, target
            )
            target_fd = os.open(target.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            if _identity(os.fstat(target_fd)) != target_state.identity:
                raise derivation_error(
                    "Publication target identity changed while opening", path=target
                )

        observed_head = read_head_before_publish(workspace)
        if observed_head != source_commit:
            raise MemError(
                "CANONICAL_HEAD_CHANGED",
                "derive",
                f"Canonical HEAD changed before {label} publication",
                canonical_changed=False,
                retry_safe=True,
                value={"source_commit": source_commit, "head": observed_head},
                recovery=["Run pco derive all again"],
            )

        _require_public_directory(parent, parent_identity)
        process.require_bound()
        _require_named_directory(
            process.root_fd,
            source_name,
            source_identity,
            process.tmp_path / process.name / source_name,
        )
        if target_fd is not None:
            _require_named_directory(
                parent_fd, target.name, target_state.identity, target
            )
            detached_name = f".discard-{uuid.uuid4().hex}"
            try:
                _detach_directory_noreplace(
                    parent_fd,
                    target.name,
                    process.root_fd,
                    detached_name,
                    target_state.identity,
                    source_display=target,
                    detached_display=process.tmp_path / process.name / detached_name,
                )
            except (OSError, TypeError, ValueError, MemError):
                # An identity mismatch can mean an unowned replacement was
                # atomically moved out of the public name.  Preserve the whole
                # process root as a recovery remnant instead of deleting it.
                process.preserve_on_cleanup = True
                raise
            try:
                _erase_owned_directory(target_fd)
                _require_named_directory(
                    process.root_fd,
                    detached_name,
                    target_state.identity,
                    process.tmp_path / process.name / detached_name,
                )
                os.rmdir(detached_name, dir_fd=process.root_fd)
            except (OSError, TypeError, ValueError, MemError):
                # Do not retry cleanup with a fresh authorization set after a
                # retained target subtree changed during its first cleanup.
                process.preserve_on_cleanup = True
                raise

        _require_public_directory(parent, parent_identity)
        process.require_bound()
        _require_named_directory(
            process.root_fd,
            source_name,
            source_identity,
            process.tmp_path / process.name / source_name,
        )
        _require_name_absent(parent_fd, target.name, target)
        atomic_noreplace_rename(process.root_fd, source_name, parent_fd, target.name)
        _require_named_directory(parent_fd, target.name, source_identity, target)
        _require_name_absent(
            process.root_fd,
            source_name,
            process.tmp_path / process.name / source_name,
        )
        _require_public_directory(parent, parent_identity)
    finally:
        if target_fd is not None:
            os.close(target_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def atomic_noreplace_rename(
    source_parent_fd: int,
    source_name: str,
    target_parent_fd: int,
    target_name: str,
) -> None:
    """Atomically publish one directory only while the target name is absent."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
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
            source_parent_fd,
            os.fsencode(source_name),
            target_parent_fd,
            os.fsencode(target_name),
            _RENAME_NOREPLACE,
        )
        != 0
    ):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), target_name)


def _detach_directory_noreplace(
    source_parent_fd: int,
    source_name: str,
    detached_parent_fd: int,
    detached_name: str,
    expected_identity: tuple[int, int],
    *,
    source_display: Path,
    detached_display: Path,
) -> None:
    """Atomically detach one identity-bound directory without deleting a racer."""

    _require_named_directory(
        source_parent_fd, source_name, expected_identity, source_display
    )
    _require_name_absent(detached_parent_fd, detached_name, detached_display)
    failure: BaseException | None = None
    try:
        atomic_noreplace_rename(
            source_parent_fd, source_name, detached_parent_fd, detached_name
        )
    except Exception as exc:
        failure = exc

    source_identity = _named_directory_identity(source_parent_fd, source_name)
    detached_identity = _named_directory_identity(detached_parent_fd, detached_name)
    if source_identity is None and detached_identity == expected_identity:
        return
    if failure is not None:
        raise failure
    raise OSError(
        errno.ESTALE,
        "directory identity changed during atomic detach; detached entry was preserved",
        str(detached_display),
    )


def _erase_owned_directory(directory_fd: int) -> None:
    """Erase one retained directory tree without following or reauthorizing names."""

    try:
        initial_names = os.listdir(directory_fd)
    except OSError as exc:
        raise OSError(f"owned directory cannot be enumerated: {exc}") from exc
    authorizations: dict[str, tuple[tuple[int, int], int]] = {}
    rejected: set[str] = set()
    removed: set[str] = set()
    errors: list[str] = []
    for name in initial_names:
        if not _safe_name(name):
            rejected.add(name)
            errors.append("unsafe owned entry name")
            continue
        try:
            observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            rejected.add(name)
            errors.append(str(exc))
            continue
        authorizations[name] = (_identity(observed), stat.S_IFMT(observed.st_mode))

    for _ in range(_CLEANUP_MAX_PASSES):
        progress = False
        for name, (identity, entry_type) in authorizations.items():
            if name in rejected or name in removed:
                continue
            try:
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                rejected.add(name)
                errors.append(str(exc))
                continue
            if (
                _identity(current) != identity
                or stat.S_IFMT(current.st_mode) != entry_type
            ):
                rejected.add(name)
                errors.append("owned entry identity changed")
                continue
            if stat.S_ISDIR(current.st_mode):
                child_fd: int | None = None
                try:
                    child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                    if _identity(os.fstat(child_fd)) != identity:
                        raise OSError("owned directory identity changed while opening")
                    _erase_owned_directory(child_fd)
                    _require_named_directory_fd(directory_fd, name, identity)
                    os.rmdir(name, dir_fd=directory_fd)
                    removed.add(name)
                    progress = True
                except OSError as exc:
                    rejected.add(name)
                    errors.append(str(exc))
                finally:
                    if child_fd is not None:
                        os.close(child_fd)
            else:
                try:
                    os.unlink(name, dir_fd=directory_fd)
                    removed.add(name)
                    progress = True
                except OSError as exc:
                    rejected.add(name)
                    errors.append(str(exc))
        try:
            if not os.listdir(directory_fd):
                return
        except OSError as exc:
            errors.append(str(exc))
            break
        if not progress:
            break
    detail = "; ".join(errors[-4:]) or "cleanup made no progress"
    raise OSError(f"owned directory could not be emptied: {detail}")


def _require_named_directory_fd(
    parent_fd: int, name: str, identity: tuple[int, int]
) -> None:
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise OSError(f"owned directory name changed: {exc}") from exc
    if not stat.S_ISDIR(observed.st_mode) or _identity(observed) != identity:
        raise OSError("owned directory identity changed")


def _require_named_directory(
    parent_fd: int,
    name: str,
    identity: tuple[int, int] | None,
    display: Path,
) -> None:
    if identity is None:
        raise derivation_error("Missing expected directory identity", path=display)
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except (OSError, TypeError, ValueError) as exc:
        raise derivation_error(
            "Directory name changed while retained", path=display, value=str(exc)
        ) from exc
    if not stat.S_ISDIR(observed.st_mode) or _identity(observed) != identity:
        raise derivation_error(
            "Directory identity changed while retained", path=display
        )


def _named_directory_matches(
    parent_fd: int, name: str, identity: tuple[int, int]
) -> bool:
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(observed.st_mode) and _identity(observed) == identity


def _named_directory_identity(parent_fd: int, name: str) -> tuple[int, int] | None:
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(observed.st_mode):
        return (-1, -1)
    return _identity(observed)


def _require_name_absent(parent_fd: int, name: str, display: Path) -> None:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except (OSError, TypeError, ValueError) as exc:
        raise derivation_error(
            "Publication name cannot be inspected safely",
            path=display,
            value=str(exc),
        ) from exc
    raise derivation_error(
        "Publication name changed before no-replace rename", path=display
    )


def _require_public_directory(path: Path, identity: tuple[int, int]) -> None:
    try:
        observed = os.lstat(path)
    except (OSError, TypeError, ValueError) as exc:
        raise derivation_error(
            "Retained directory public identity changed", path=path, value=str(exc)
        ) from exc
    if not stat.S_ISDIR(observed.st_mode) or _identity(observed) != identity:
        raise derivation_error("Retained directory public identity changed", path=path)


def _identity(observed: os.stat_result) -> tuple[int, int]:
    return observed.st_dev, observed.st_ino


def _safe_name(name: str) -> bool:
    try:
        name.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return bool(
        name
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and not any(ord(character) < 32 for character in name)
    )
