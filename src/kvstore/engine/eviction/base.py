"""Eviction policy interface.

The store notifies the policy about every insert, access and removal; when
the store is over a limit it asks the policy for a victim and removes it.
The policy only tracks keys -- it never owns data -- so the store stays the
single source of truth.

``protect`` holds the keys of the command that pushed the store over its
limit: a command never evicts the key it just wrote.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Container
from typing import ClassVar


class EvictionPolicy(ABC):
    name: ClassVar[str]
    evicts: ClassVar[bool] = True
    """False for ``noeviction``: over the limit, writes fail with OOM instead."""

    @abstractmethod
    def on_insert(self, key: str) -> None: ...

    @abstractmethod
    def on_access(self, key: str) -> None: ...

    @abstractmethod
    def on_remove(self, key: str) -> None: ...

    @abstractmethod
    def victim(self, protect: Container[str] = ()) -> str | None:
        """The key to evict next, or ``None`` if nothing can be evicted."""

    @abstractmethod
    def clear(self) -> None: ...

    @abstractmethod
    def __len__(self) -> int: ...
