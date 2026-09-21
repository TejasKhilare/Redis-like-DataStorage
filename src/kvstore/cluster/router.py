"""Stateless router: sends each command to the shard that owns its key(s)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from kvstore.cluster.hash_ring import ConsistentHashRing
from kvstore.core.exceptions import (
    CommandError,
    CrossShardError,
    InvalidArgumentError,
    KVStoreError,
    UnknownCommandError,
)
from kvstore.engine.commands import COMMANDS
from kvstore.protocol.client import KVClient


@dataclass(frozen=True, slots=True)
class NodeStatus:
    address: str
    healthy: bool
    latency_ms: float | None = None
    error: str | None = None


class ShardRouter:
    """Routes by consistent hashing; holds no data, so any number can run.

    The router reads key positions from the shared command table and checks
    arity itself, so bad requests never cost a network hop.
    """

    def __init__(self, shards: list[str], *, virtual_nodes: int = 100, timeout_s: float = 2.0):
        self.ring = ConsistentHashRing(shards, virtual_nodes=virtual_nodes)
        self._clients = {
            address: KVClient.from_address(address, timeout_s=timeout_s) for address in shards
        }

    def owner(self, key: str) -> str:
        return self.ring.get_node(key)

    async def execute(self, command: str, *args: Any) -> Any:
        spec = COMMANDS.get(command.upper()) if isinstance(command, str) else None
        if spec is None:
            raise UnknownCommandError(f"unknown command '{command}'")
        spec.check_arity(args)

        keys = spec.keys(args)
        if not keys:
            if spec.name == "PING":
                return args[0] if args else "PONG"
            raise CommandError(f"'{spec.name}' is not supported through the router")
        if not all(isinstance(key, str) for key in keys):
            raise InvalidArgumentError("key must be a string")

        owners = {self.owner(key) for key in keys}
        if len(owners) > 1:
            # Same rule as Redis Cluster's CROSSSLOT: no cross-shard atomicity.
            raise CrossShardError("keys in the request belong to different shards")
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
