"""Eviction policy interface.

The store notifies the policy about every insert, access and removal; when the
store is over capacity it asks the policy for a victim and removes it. The
policy only tracks keys -- it never owns data -- so the store stays the single
source of truth.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar


class EvictionPolicy(ABC):
    name: ClassVar[str]

    @abstractmethod
    def on_insert(self, key: str) -> None: ...

    @abstractmethod
    def on_access(self, key: str) -> None: ...

    @abstractmethod
    def on_remove(self, key: str) -> None: ...

    @abstractmethod
    def victim(self) -> str | None:
        """The key to evict next, or ``None`` when nothing is tracked."""

    @abstractmethod
    def __len__(self) -> int: ...
