from __future__ import annotations

import errno
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import MemError


if os.name == "nt":
    import msvcrt
else:
    import fcntl


def _lock_descriptor(descriptor: int, *, blocking: bool) -> None:
    if os.name == "nt":
        # Windows byte-range locks require a byte in the file and operate from
        # the current file position.
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BlockingIOError(exc.errno, exc.strerror) from exc
            raise
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
                retryable=True,
                recovery=["Retry pco proposal commit after the active command finishes"],
            ) from exc
        acquired = True
        yield
    finally:
        try:
            if acquired:
                _unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)
