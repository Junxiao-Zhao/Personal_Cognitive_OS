from __future__ import annotations

import pytest

from mem_core.errors import MemError
from mem_core.repository import MemoryRepository


def test_repository_lock_rejects_second_nonblocking_owner(repository: MemoryRepository) -> None:
    with repository.lock():
        with pytest.raises(MemError) as exc:
            with repository.lock(blocking=False):
                pass
    assert exc.value.detail.code == "CANONICAL_LOCKED"


def test_repository_lock_file_does_not_dirty_git(repository: MemoryRepository) -> None:
    with repository.lock():
        assert repository.is_clean()
