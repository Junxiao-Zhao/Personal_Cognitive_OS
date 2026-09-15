from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .errors import MemError

if os.name == "nt":
    import msvcrt

    WINDOWS_LOCK_MODE = msvcrt.LK_LOCK
    WINDOWS_NONBLOCK_MODE = msvcrt.LK_NBLCK
else:
    import fcntl

    # Keep the helper testable on POSIX without importing Windows-only names.
    WINDOWS_LOCK_MODE = 1
    WINDOWS_NONBLOCK_MODE = 2


WINDOWS_LOCK_RETRY_SECONDS = 0.1
_WINDOWS_CONTENTION_ERRNOS = {
    errno.EACCES,
    errno.EAGAIN,
    errno.EDEADLK,
    getattr(errno, "EDEADLOCK", errno.EDEADLK),
    getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
}


def _acquire_windows_lock(
    descriptor: int,
    *,
    blocking: bool,
    locking: Callable[[int, int, int], None],
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Acquire a Windows byte-range lock, retrying finite CRT blocking calls."""

    mode = WINDOWS_LOCK_MODE if blocking else WINDOWS_NONBLOCK_MODE
    while True:
        try:
            locking(descriptor, mode, 1)
        except OSError as exc:
            if exc.errno not in _WINDOWS_CONTENTION_ERRNOS:
                raise
            if not blocking:
                raise BlockingIOError(exc.errno, exc.strerror) from exc
            sleep(WINDOWS_LOCK_RETRY_SECONDS)
        else:
            return


def _lock_descriptor(descriptor: int, *, blocking: bool) -> None:
    if os.name == "nt":
        # Windows byte-range locks require a byte in the file and operate from
        # the current file position.
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
        _acquire_windows_lock(descriptor, blocking=blocking, locking=msvcrt.locking)
        return

    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    fcntl.flock(descriptor, flags)


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def canonical_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Hold an OS-released exclusive lock for a canonical transaction."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        try:
            _lock_descriptor(descriptor, blocking=blocking)
        except BlockingIOError as exc:
            raise MemError(
                "CANONICAL_LOCKED",
                "canonical_lock",
                "Canonical transaction is already running",
                canonical_changed=False,
                retry_safe=True,
                recovery=[
                    "Retry pco proposal commit after the active command finishes"
                ],
            ) from exc
        acquired = True
        yield
    finally:
        try:
            if acquired:
                _unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)
