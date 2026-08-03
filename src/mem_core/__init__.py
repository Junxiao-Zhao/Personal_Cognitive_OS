"""Profile-neutral, append-only canonical memory core."""

from .errors import MemError
from .profile import Profile, ProfileRegistry
from .repository import MemoryRepository
from .transaction import TransactionManager

__all__ = [
    "MemError",
    "MemoryRepository",
    "Profile",
    "ProfileRegistry",
    "TransactionManager",
]

__version__ = "0.1.0"
