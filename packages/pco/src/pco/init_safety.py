"""Descriptor-anchored filesystem primitives for workspace initialization."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping

from mem_core.errors import MemError, ensure
from mem_core.profile import Profile

try:
    import fcntl
except ImportError:  # pragma: no cover - capability-gated non-POSIX import
    fcntl = None  # type: ignore[assignment]


Identity = tuple[int, int]


def _unsafe(message: str, *, value: Any = None) -> MemError:
    return MemError(
        "WORKSPACE_PATH_UNSAFE",
        "init",
        message,
        value=value,
        canonical_changed=False,
        retry_safe=False,
    )


def _race(message: str) -> MemError:
    return MemError(
        "WORKSPACE_INIT_RACE",
        "init",
        message,
        canonical_changed=False,
        retry_safe=False,
    )


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Encode a canonical JSON object with one trailing line feed."""

    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def lstat_final(path: Path) -> os.stat_result | None:
    """Inspect the final path component without resolving or following it."""

    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _unsafe(
            "Workspace target cannot be inspected safely", value=str(exc)
        ) from exc


def lstat_absent_or_real_directory(path: Path) -> os.stat_result | None:
    observed = lstat_final(path)
    if observed is None:
        return None
    ensure(
        stat.S_ISDIR(observed.st_mode),
        "WORKSPACE_PATH_UNSAFE",
        "init",
        "Workspace target must be absent or a real directory",
        path=str(path),
        canonical_changed=False,
        retry_safe=False,
    )
    return observed


def fstat_identity(descriptor: int) -> Identity:
    observed = os.fstat(descriptor)
    return observed.st_dev, observed.st_ino


def _stat_identity(observed: os.stat_result) -> Identity:
    return observed.st_dev, observed.st_ino


def require_anchor_capabilities_before_mutation() -> None:
    """Reject platforms lacking the primitives required by the safety model."""

    supported_dir_fd = getattr(os, "supports_dir_fd", set())
    supported_fd = getattr(os, "supports_fd", set())
    required = (
        os.name == "posix",
        hasattr(os, "O_DIRECTORY"),
        hasattr(os, "O_NOFOLLOW"),
        hasattr(os, "O_PATH"),
        hasattr(os, "fchmod"),
        fcntl is not None,
        hasattr(fcntl, "flock"),
        hasattr(fcntl, "LOCK_EX"),
        hasattr(fcntl, "LOCK_NB"),
        os.open in supported_dir_fd,
        os.mkdir in supported_dir_fd,
        os.stat in supported_dir_fd,
        os.stat in getattr(os, "supports_follow_symlinks", set()),
        os.listdir in supported_fd,
        Path("/proc/self/fd").is_dir(),
    )
    if not all(required):
        raise _unsafe(
            "Platform does not support descriptor-anchored workspace initialization"
        )


def ensure_directory_identity(
    directory_fd: int,
    target: Path,
    *,
    expected_identity: Identity | None = None,
) -> None:
    """Ensure the name still denotes the real directory held by ``directory_fd``."""

    anchored = fstat_identity(directory_fd)
    if expected_identity is not None and anchored != expected_identity:
        raise _race("Workspace directory descriptor identity changed")
    try:
        observed = lstat_final(target)
    except (MemError, OSError) as exc:
        raise _race("Workspace target could not be re-inspected") from exc
    if (
        observed is None
        or not stat.S_ISDIR(observed.st_mode)
        or _stat_identity(observed) != anchored
    ):
        raise _race("Workspace target identity changed during initialization")


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _acquire_initializer_lock(
    directory_fd: int,
    *,
    capability_probe: bool,
) -> None:
    if fcntl is None:
        raise _unsafe("Exclusive workspace initialization locks are unavailable")
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise _race("Another initializer holds the workspace lock") from exc
    except OSError as exc:
        if capability_probe:
            raise _unsafe(
                "Workspace filesystem does not support initialization locks",
                value=str(exc),
            ) from exc
        raise _race("Workspace initialization lock failed") from exc


def _probe_parent_lock_support(target: Path) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    try:
        parent_fd = os.open(target.parent, flags)
    except OSError as exc:
        raise _unsafe(
            "Workspace target parent cannot be opened safely", value=str(exc)
        ) from exc
    try:
        _acquire_initializer_lock(parent_fd, capability_probe=True)
    finally:
        os.close(parent_fd)


def open_directory_no_follow(
    target: Path,
    *,
    create_if_absent: bool,
    inspected: os.stat_result | None = None,
) -> int:
    """Open a real final-component directory and optionally create it once."""

    if create_if_absent:
        try:
            os.mkdir(target, mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise _unsafe("Unable to create workspace target", value=str(exc)) from exc
    try:
        descriptor = os.open(target, _directory_flags())
    except OSError as exc:
        raise _race("Workspace target changed while it was being opened") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise _unsafe("Workspace target is not a directory")
        if inspected is not None and fstat_identity(descriptor) != _stat_identity(inspected):
            raise _race("Workspace target changed while it was being opened")
        ensure_directory_identity(descriptor, target)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def ensure_directory_empty(directory_fd: int) -> None:
    try:
        entries = os.listdir(directory_fd)
    except OSError as exc:
        raise _race("Workspace target could not be enumerated") from exc
    ensure(
        not entries,
        "WORKSPACE_NOT_EMPTY",
        "init",
        "Workspace target must be empty",
        canonical_changed=False,
        retry_safe=False,
    )


def _relative_parts(relative: str | Path | PurePosixPath, *, allow_dot: bool) -> tuple[str, ...]:
    raw = str(relative)
    candidate = PurePosixPath(raw)
    unsafe = (
        not raw
        or candidate.is_absolute()
        or ".." in candidate.parts
        or raw.startswith("//")
    )
    if unsafe:
        raise _unsafe("Anchored paths must be safe relative paths", value=raw)
    parts = tuple(part for part in candidate.parts if part != ".")
    if not parts and not allow_dot:
        raise _unsafe("Anchored path must name an entry", value=raw)
    return parts


class DirectoryAnchor:
    """An owned directory descriptor used for no-follow relative operations."""

    def __init__(
        self,
        descriptor: int,
        *,
        expected_identity: Identity,
        display_path: Path,
    ) -> None:
        self._descriptor = descriptor
        self.expected_identity = expected_identity
        self.display_path = display_path
        self._closed = False
        self._retained: dict[tuple[str, ...], tuple[int, Identity, Path]] = {}

    def __enter__(self) -> "DirectoryAnchor":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def descriptor(self) -> int:
        if self._closed:
            raise _race("Directory anchor is closed")
        return self._descriptor

    def close(self) -> None:
        if not self._closed:
            for descriptor, _identity, _display_path in self._retained.values():
                os.close(descriptor)
            self._retained.clear()
            os.close(self._descriptor)
            self._closed = True

    def assert_target_identity(self) -> None:
        ensure_directory_identity(
            self.descriptor,
            self.display_path,
            expected_identity=self.expected_identity,
        )

    def _open_directory_descriptor(
        self,
        relative: str | Path | PurePosixPath,
        *,
        create: bool,
    ) -> int:
        parts = _relative_parts(relative, allow_dot=True)
        current = os.dup(self.descriptor)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                    except OSError as exc:
                        raise _unsafe(
                            "Unable to create anchored directory", value=str(relative)
                        ) from exc
                try:
                    following = os.open(part, _directory_flags(), dir_fd=current)
                except OSError as exc:
                    raise _unsafe(
                        "Anchored directory traversal encountered an unsafe entry",
                        value=str(relative),
                    ) from exc
                os.close(current)
                current = following
            return current
        except BaseException:
            os.close(current)
            raise

    def mkdir(
        self,
        relative: str | Path | PurePosixPath,
        *,
        parents: bool = False,
    ) -> None:
        parts = _relative_parts(relative, allow_dot=False)
        if parents:
            descriptor = self._open_directory_descriptor(relative, create=True)
            os.close(descriptor)
            return
        parent = self._open_directory_descriptor(
            PurePosixPath(*parts[:-1]) if len(parts) > 1 else ".",
            create=False,
        )
        try:
            try:
                os.mkdir(parts[-1], mode=0o700, dir_fd=parent)
            except FileExistsError:
                try:
                    existing = os.open(parts[-1], _directory_flags(), dir_fd=parent)
                except OSError as exc:
                    raise _unsafe(
                        "Anchored directory path is not a real directory",
                        value=str(relative),
                    ) from exc
                else:
                    os.close(existing)
            except OSError as exc:
                raise _unsafe(
                    "Unable to create anchored directory", value=str(relative)
                ) from exc
        finally:
            os.close(parent)

    def open_directory(
        self, relative: str | Path | PurePosixPath
    ) -> "DirectoryAnchor":
        parts = _relative_parts(relative, allow_dot=True)
        retained = self._retained.get(parts)
        if retained is None:
            descriptor = self._open_directory_descriptor(relative, create=False)
            expected_identity = fstat_identity(descriptor)
            display_path = self.display_path / str(relative)
        else:
            retained_descriptor, expected_identity, display_path = retained
            descriptor = os.dup(retained_descriptor)
        return DirectoryAnchor(
            descriptor,
            expected_identity=expected_identity,
            display_path=display_path,
        )

    def retain_directory(self, relative: str | Path | PurePosixPath) -> None:
        """Keep a named child descriptor alive for the lifetime of this anchor."""

        parts = _relative_parts(relative, allow_dot=False)
        if parts in self._retained:
            return
        descriptor = self._open_directory_descriptor(relative, create=False)
        expected_identity = fstat_identity(descriptor)
        display_path = self.display_path / str(relative)
        try:
            ensure_directory_identity(
                descriptor,
                display_path,
                expected_identity=expected_identity,
            )
        except BaseException:
            os.close(descriptor)
            raise
        self._retained[parts] = (descriptor, expected_identity, display_path)

    def _open_parent(
        self,
        relative: str | Path | PurePosixPath,
        *,
        parents: bool,
    ) -> tuple[int, str]:
        parts = _relative_parts(relative, allow_dot=False)
        parent_relative = PurePosixPath(*parts[:-1]) if len(parts) > 1 else PurePosixPath(".")
        return (
            self._open_directory_descriptor(parent_relative, create=parents),
            parts[-1],
        )

    def touch(
        self,
        relative: str | Path | PurePosixPath,
        *,
        parents: bool = False,
    ) -> None:
        self.write_bytes(relative, b"", parents=parents)

    def write_bytes(
        self,
        relative: str | Path | PurePosixPath,
        content: bytes,
        *,
        parents: bool = False,
        mode: int = 0o600,
    ) -> None:
        if not hasattr(os, "fchmod"):
            raise _unsafe("Safe anchored file mode setting is unavailable")
        parent, name = self._open_parent(relative, parents=parents)
        descriptor: int | None = None
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                descriptor = os.open(name, flags, mode, dir_fd=parent)
            except OSError as exc:
                raise _unsafe(
                    "Unable to create anchored regular file", value=str(relative)
                ) from exc
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fchmod(descriptor, mode)
        except OSError as exc:
            raise _unsafe("Unable to write anchored regular file", value=str(relative)) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent)

    def chmod(self, relative: str | Path | PurePosixPath, mode: int) -> None:
        safe_stat = (
            os.stat in getattr(os, "supports_dir_fd", set())
            and os.stat in getattr(os, "supports_follow_symlinks", set())
        )
        if (
            not hasattr(os, "O_PATH")
            or not hasattr(os, "fchmod")
            or not safe_stat
            or not Path("/proc/self/fd").is_dir()
        ):
            raise _unsafe("Safe anchored chmod is unavailable")
        parent, name = self._open_parent(relative, parents=False)
        path_descriptor: int | None = None
        descriptor: int | None = None
        try:
            path_descriptor = os.open(
                name,
                os.O_PATH | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent,
            )
            path_stat = os.fstat(path_descriptor)
            if not stat.S_ISREG(path_stat.st_mode):
                raise _unsafe("Anchored chmod target must be a regular file")
            observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _stat_identity(observed) != _stat_identity(path_stat):
                raise _unsafe("Anchored chmod target identity changed")
            descriptor = os.open(
                f"/proc/self/fd/{path_descriptor}",
                os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
            )
            if fstat_identity(descriptor) != _stat_identity(path_stat):
                raise _unsafe("Anchored chmod target identity changed")
            os.fchmod(descriptor, mode)
        except OSError as exc:
            raise _unsafe("Unable to chmod anchored regular file", value=str(relative)) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if path_descriptor is not None:
                os.close(path_descriptor)
            os.close(parent)

    def git_cwd(self, relative: str | Path | PurePosixPath) -> "GitWorkingDirectory":
        return GitWorkingDirectory(self.open_directory(relative))


@dataclass(slots=True)
class GitWorkingDirectory:
    anchor: DirectoryAnchor

    @property
    def cwd(self) -> str:
        return f"/proc/self/fd/{self.anchor.descriptor}"

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return (self.anchor.descriptor,)

    def close(self) -> None:
        self.anchor.close()


class AnchoredGit:
    """Git runner whose subprocess cwd is derived only from an open descriptor."""

    def __init__(self, working_directory: GitWorkingDirectory) -> None:
        self.working_directory = working_directory

    def __enter__(self) -> "AnchoredGit":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.working_directory.close()

    def run(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.working_directory.cwd,
            pass_fds=self.working_directory.pass_fds,
            text=True,
            capture_output=True,
            check=False,
            env=os.environ.copy(),
        )
        if check and result.returncode:
            raise MemError(
                "GIT_FAILED",
                "git",
                result.stderr.strip() or result.stdout.strip(),
                canonical_changed=False,
                retry_safe=True,
                recovery=[
                    "Remove the partial workspace or initialize a different target"
                ],
            )
        return result.stdout.strip()


def _managed_pre_commit_hook_bytes() -> bytes:
    executable = shlex.quote(sys.executable)
    return (
        "#!/bin/sh\n"
        "# managed-by: mem-core\n"
        f'exec {executable} -m mem_core.hook --repo "$(git rev-parse --show-toplevel)"\n'
    ).encode("utf-8")


def install_pre_commit_hook_anchored(
    root: DirectoryAnchor, git: AnchoredGit
) -> None:
    """Install the managed validation hook through the canonical root anchor."""

    common_git_dir = git.run("rev-parse", "--git-common-dir")
    _relative_parts(common_git_dir, allow_dot=False)
    with root.open_directory(common_git_dir) as git_root:
        git_root.mkdir("hooks", parents=True)
        git_root.write_bytes(
            "hooks/pre-commit", _managed_pre_commit_hook_bytes(), mode=0o755
        )


def _read_regular_file_no_follow(path: Path) -> bytes:
    try:
        observed = os.lstat(path)
    except OSError as exc:
        raise _unsafe("Unable to inspect bundled resource", value=str(path)) from exc
    if not stat.S_ISREG(observed.st_mode):
        raise _unsafe("Bundled resource is not a regular file", value=str(path))
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        )
    except OSError as exc:
        raise _unsafe("Unable to open bundled resource", value=str(path)) from exc
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def copy_regular_tree_anchored(root: DirectoryAnchor, source: Path) -> None:
    """Copy a trusted resource tree without following non-regular entries."""

    pending: list[tuple[Path, PurePosixPath]] = [(source, PurePosixPath("."))]
    while pending:
        source_directory, relative_directory = pending.pop()
        try:
            entries = sorted(os.scandir(source_directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise _unsafe(
                "Unable to enumerate bundled resource tree", value=str(source_directory)
            ) from exc
        for entry in entries:
            try:
                observed = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise _unsafe("Unable to inspect bundled resource", value=entry.path) from exc
            relative = relative_directory / entry.name
            if stat.S_ISDIR(observed.st_mode):
                root.mkdir(relative, parents=True)
                pending.append((Path(entry.path), relative))
            elif stat.S_ISREG(observed.st_mode):
                root.write_bytes(
                    relative,
                    _read_regular_file_no_follow(Path(entry.path)),
                    parents=True,
                )
            else:
                raise _unsafe("Bundled resource tree contains an unsafe entry", value=entry.path)


def copy_tree_bytes_anchored(
    root: DirectoryAnchor, files_by_path: Mapping[str, bytes]
) -> None:
    """Install an already-verified regular-file snapshot through ``root``."""

    for relative, content in sorted(files_by_path.items()):
        root.write_bytes(relative, content, parents=True)


def copy_profile_tree_anchored(root: DirectoryAnchor, profile: Profile) -> None:
    copy_regular_tree_anchored(root, profile.root)


@contextmanager
def claim_workspace(target: Path) -> Iterator[DirectoryAnchor]:
    """Serialize initializers, then create and retain the ``.pco`` claim."""

    require_anchor_capabilities_before_mutation()
    stat_before = lstat_absent_or_real_directory(target)
    if stat_before is None:
        _probe_parent_lock_support(target)
    directory_fd = open_directory_no_follow(
        target,
        create_if_absent=stat_before is None,
        inspected=stat_before,
    )
    anchor = DirectoryAnchor(
        directory_fd,
        expected_identity=fstat_identity(directory_fd),
        display_path=target,
    )
    try:
        _acquire_initializer_lock(
            directory_fd,
            capability_probe=stat_before is not None,
        )
        anchor.assert_target_identity()
        ensure_directory_empty(directory_fd)
        try:
            os.mkdir(".pco", mode=0o700, dir_fd=directory_fd)
        except FileExistsError as exc:
            raise _race("Another initializer claimed .pco") from exc
        except OSError as exc:
            raise _race("Unable to claim .pco") from exc
        anchor.retain_directory(".pco")
        try:
            entries = os.listdir(directory_fd)
        except OSError as exc:
            raise _race("Workspace changed after .pco was claimed") from exc
        if entries != [".pco"]:
            raise _race("Workspace changed while .pco was being claimed")
        anchor.assert_target_identity()
        with anchor.open_directory(".pco") as claimed:
            claimed.assert_target_identity()
        yield anchor
    finally:
        anchor.close()
