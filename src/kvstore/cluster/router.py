"""Stateless router: sends each command to the shard that owns its key(s)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from kvstore.cluster.hash_ring import ConsistentHashRing
from kvstore.core.exceptions import (
    CommandError,
    CrossShardError,
    InvalidArgumentError,
    KVStoreError,
    UnknownCommandError,
)
from kvstore.engine.commands import COMMANDS, STATELESS, CommandContext
from kvstore.protocol.client import KVClient
from kvstore.protocol.resp import OK


@dataclass(frozen=True, slots=True)
class NodeStatus:
    address: str
    healthy: bool
    latency_ms: float | None = None
    error: str | None = None


def _sum(results: list[Any]) -> Any:
    return sum(results)


def _concat(results: list[Any]) -> Any:
    return [item for result in results for item in result]


def _ok(results: list[Any]) -> Any:
    return OK


# Keyless commands the router runs on every shard, and how it merges the replies.
FANOUT: dict[str, Callable[[list[Any]], Any]] = {
    "DBSIZE": _sum,
    "KEYS": _concat,
    "FLUSHALL": _ok,
    "FLUSHDB": _ok,
    "BGSAVE": _ok,
    "BGREWRITEAOF": _ok,
    "SAVE": _ok,
}


def hash_slot_key(key: str) -> str:
    """The part of a key that is hashed. ``{user:1}:cart`` hashes as ``user:1``.

    Redis Cluster's hash tags: keys sharing a tag always land on the same
    shard, so multi-key commands on them are allowed.
    """
    start = key.find("{")
    if start >= 0:
        end = key.find("}", start + 1)
        if end > start + 1:
            return key[start + 1 : end]
    return key


class ShardRouter:
    """Routes by consistent hashing; holds no data, so any number can run.

    The router reads key positions and flags from the shared command table,
    checks arity itself (bad requests never cost a network hop), answers
    stateless commands locally and fans keyless admin commands out.
    """

    def __init__(self, shards: list[str], *, virtual_nodes: int = 100, timeout_s: float = 2.0):
        self.ring = ConsistentHashRing(shards, virtual_nodes=virtual_nodes)
        self._clients = {
            address: KVClient.from_address(address, timeout_s=timeout_s) for address in shards
        }

    def owner(self, key: str) -> str:
        return self.ring.get_node(hash_slot_key(key))

    async def execute(self, command: str, *args: Any) -> Any:
        spec = COMMANDS.get(command.upper()) if isinstance(command, str) else None
        if spec is None:
            raise UnknownCommandError(f"unknown command '{command}'")
        spec.check_arity(args)

        if STATELESS in spec.flags:
            # PING, ECHO, CLIENT, COMMAND...: no keyspace needed.
            return spec.handler(cast(CommandContext, None), list(args))

        keys = spec.keys(args)
        if not keys:
            merge = FANOUT.get(spec.name)
            if merge is None:
                raise CommandError(f"'{spec.name}' is not supported through the router")
            results = await asyncio.gather(
                *(client.execute(command, *args) for client in self._clients.values())
            )
            return merge(list(results))

        if not all(isinstance(key, str) for key in keys):
            raise InvalidArgumentError("key must be a string")
        owners = {self.owner(key) for key in keys}
        if len(owners) > 1:
            # Same rule as Redis Cluster: no cross-shard atomicity, use hash tags instead.
            raise CrossShardError("Keys in request don't hash to the same shard")
        return await self._clients[owners.pop()].execute(command, *args)

    async def node_status(self) -> list[NodeStatus]:
        return list(await asyncio.gather(*(self._ping(c) for c in self._clients.values())))

    async def close(self) -> None:
        await asyncio.gather(*(client.close() for client in self._clients.values()))

    @staticmethod
    async def _ping(client: KVClient) -> NodeStatus:
        start = time.perf_counter()
        try:
            await client.execute("PING")
        except KVStoreError as exc:
            return NodeStatus(client.address, healthy=False, error=exc.message)
        latency_ms = round((time.perf_counter() - start) * 1000, 3)
        return NodeStatus(client.address, healthy=True, latency_ms=latency_ms)
