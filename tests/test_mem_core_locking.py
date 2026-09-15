from __future__ import annotations

import errno

import pytest

from mem_core.errors import MemError
from mem_core import locking
from mem_core.repository import MemoryRepository


def test_repository_lock_rejects_second_nonblocking_owner(
    repository: MemoryRepository,
) -> None:
    with repository.lock():
        with pytest.raises(MemError) as exc:
            with repository.lock(blocking=False):
                pass
    assert exc.value.detail.code == "CANONICAL_LOCKED"
    assert exc.value.detail.canonical_changed is False
    assert exc.value.detail.retry_safe is True
    assert exc.value.detail.recovery == [
        "Retry pco proposal commit after the active command finishes"
    ]


def test_repository_lock_file_does_not_dirty_git(repository: MemoryRepository) -> None:
    with repository.lock():
        assert repository.is_clean()


@pytest.mark.parametrize(
    "contention_errno", [errno.EACCES, errno.EAGAIN, errno.EDEADLK]
)
def test_windows_blocking_lock_retries_crt_contention_until_available(
    contention_errno: int,
) -> None:
    """Catches a Windows blocking path that lets finite LK_LOCK retries escape."""

    attempts = 0
    delays: list[float] = []

    def fake_lock(_descriptor: int, _mode: int, _length: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError(contention_errno, "lock is busy")

    locking._acquire_windows_lock(
        1,
        blocking=True,
        locking=fake_lock,
        sleep=delays.append,
    )

    assert attempts == 3
    assert delays == [locking.WINDOWS_LOCK_RETRY_SECONDS] * 2


def test_windows_nonblocking_lock_reports_contention_without_retry() -> None:
    delays: list[float] = []

    def fake_lock(_descriptor: int, _mode: int, _length: int) -> None:
        raise OSError(errno.EDEADLK, "lock is busy")

    with pytest.raises(BlockingIOError):
        locking._acquire_windows_lock(
            1,
            blocking=False,
            locking=fake_lock,
            sleep=delays.append,
        )

    assert delays == []


def test_windows_lock_propagates_unrelated_os_errors() -> None:
    def fake_lock(_descriptor: int, _mode: int, _length: int) -> None:
        raise OSError(errno.EPERM, "permission denied")

    with pytest.raises(OSError) as exc:
        locking._acquire_windows_lock(
            1,
            blocking=True,
            locking=fake_lock,
            sleep=lambda _delay: pytest.fail("unrelated error must not be retried"),
        )

    assert exc.value.errno == errno.EPERM
