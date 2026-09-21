"""Key-value operations for the HTTP API, independent of where the data lives.

A shard serves them from its local engine; the router forwards them to the
owning shard. The endpoints depend only on :class:`KVService`, so both roles
share the same API code.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from kvstore.cluster.router import ShardRouter
from kvstore.core.exceptions import KeyNotFoundError
from kvstore.engine import Engine
from kvstore.protocol.tcp_server import GroupCommit
from kvstore.replication.node import ShardNode


class KVService(ABC):
    @abstractmethod
    async def execute(self, command: str, *args: Any) -> Any:
        """Run one raw command."""

    async def get(self, key: str) -> str:
        value: str | None = await self.execute("GET", key)
        if value is None:
            raise KeyNotFoundError(f"key '{key}' not found")
        return value

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        args: list[Any] = [key, value]
        if ttl_seconds is not None:
            args += ["EX", ttl_seconds]
        await self.execute("SET", *args)

    async def delete(self, key: str) -> None:
        if await self.execute("DEL", key) == 0:
            raise KeyNotFoundError(f"key '{key}' not found")

    async def ttl(self, key: str) -> int | None:
        """Seconds to live, or ``None`` if the key never expires."""
        remaining: int = await self.execute("TTL", key)
        if remaining == -2:
            raise KeyNotFoundError(f"key '{key}' not found")
        return None if remaining == -1 else remaining

    async def expire(self, key: str, seconds: int) -> None:
        if await self.execute("EXPIRE", key, seconds) == 0:
            raise KeyNotFoundError(f"key '{key}' not found")

    async def persist(self, key: str) -> None:
        # PERSIST answers 0 both for "missing" and "had no TTL"; only the first is an error.
        if await self.execute("PERSIST", key) == 0 and await self.execute("EXISTS", key) == 0:
            raise KeyNotFoundError(f"key '{key}' not found")

    async def key_type(self, key: str) -> str:
        kind = str(await self.execute("TYPE", key))
        if kind == "none":
            raise KeyNotFoundError(f"key '{key}' not found")
        return kind


class LocalKVService(KVService):
    """Serves from this node: its engine, through the node's role checks if it has one."""

    def __init__(self, target: Engine | ShardNode, group_commit: GroupCommit | None = None) -> None:
        self._execute: Callable[..., Any] = target.execute
        self._group_commit = group_commit

    async def execute(self, command: str, *args: Any) -> Any:
        # Commands are microseconds of CPU work, so they run inline on the
        # event loop -- serialized, like Redis -- instead of in a thread pool.
        if self._group_commit is None:
            return await _resolve(self._execute(command, *args))
        # Same rule as the RESP server: no reply before the shared commit.
        committed = self._group_commit.join()
        try:
            return await _resolve(self._execute(command, *args))
        finally:
            await committed


async def _resolve(result: Any) -> Any:
    """``WAIT`` answers asynchronously; everything else synchronously."""
    return await result if inspect.isawaitable(result) else result


class RoutedKVService(KVService):
    def __init__(self, router: ShardRouter) -> None:
        self.router = router

    async def execute(self, command: str, *args: Any) -> Any:
        return await self.router.execute(command, *args)
