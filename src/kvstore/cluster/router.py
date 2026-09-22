"""Stateless router: sends each command to the shard group that owns its key(s).

Data path, per batch of pipelined commands from one client:

1. every command is checked (arity, key positions, same-group rule) and
   routed, without a network hop for bad requests or stateless commands;
2. the commands for each shard group go out as one pipeline on one
   multiplexed connection, all groups in parallel -- a client's pipeline
   stays a pipeline instead of becoming one round trip per command;
3. replies come back in order, errors in place.

Retries are only automatic when they cannot apply a write twice: when the
request never reached the node (connection refused), when the node refused
it without running it (``READONLY`` from a demoted primary, ``TRYAGAIN``
during a key move, ``ASK`` after one), or when every command is a read.
A write that timed out is reported, not retried.

With ``read_from_replicas``, a batch of reads goes to one of the group's
healthy replicas (round-robin) instead of its primary: more read capacity,
but reads may lag the primary by the replication delay.

With ``wait_replicas`` set, every batch that writes ends with ``WAIT`` on
the same connection, and a write is acknowledged only once that many
replicas have it: a failover then loses no acknowledged write, at the cost
of a replication round trip per batch.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from kvstore.cluster.hash_ring import ConsistentHashRing, hash_slot_key
from kvstore.cluster.topology import ClusterConfig
from kvstore.core.exceptions import (
    AskRedirectError,
    CommandError,
    ConnectFailedError,
    CrossShardError,
    InvalidArgumentError,
    KVStoreError,
    NodeUnavailableError,
    NotEnoughReplicasError,
    ReadOnlyReplicaError,
    TryAgainError,
    UnknownCommandError,
)
from kvstore.engine.commands import COMMANDS, STATELESS, CommandContext, CommandSpec
from kvstore.observability.metrics import CommandStats
from kvstore.protocol.client import KVClientPool
from kvstore.protocol.resp import OK

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NodeStatus:
    address: str
    shard: str
    role: str  # "primary" | "replica"
    healthy: bool
    latency_ms: float | None = None
    error: str | None = None


def _sum(results: list[Any]) -> Any:
    return sum(results)


def _concat(results: list[Any]) -> Any:
    return [item for result in results for item in result]


def _ok(results: list[Any]) -> Any:
    return OK


# Keyless commands the router runs on every shard group, and how it merges the replies.
FANOUT: dict[str, Callable[[list[Any]], Any]] = {
    "DBSIZE": _sum,
    "KEYS": _concat,
    "FLUSHALL": _ok,
    "FLUSHDB": _ok,
    "BGSAVE": _ok,
    "BGREWRITEAOF": _ok,
    "SAVE": _ok,
}

# Replies that mean "not executed here, try again": safe to retry even for writes.
_RETRYABLE = (ReadOnlyReplicaError, TryAgainError, AskRedirectError)


@dataclass(slots=True)
class _Routed:
    """Where one command goes: a shard group, every group (fan-out), or nowhere."""

    shard: str | None = None
    fanout: bool = False
    readonly: bool = True


class ShardRouter:
    """Routes by consistent hashing over shard-group ids; holds no data.

    The configuration can be replaced at any time (failover, rebalance) with
    :meth:`apply_config`; only a newer epoch is accepted.
    """

    def __init__(
        self,
        config: ClusterConfig | Sequence[str],
        *,
        virtual_nodes: int = 100,
        timeout_s: float = 2.0,
        pool_size: int = 2,
        retries: int = 3,
        retry_backoff_s: float = 0.05,
        wait_replicas: int = 0,
        wait_timeout_s: float = 1.0,
        read_from_replicas: bool = False,
        on_stale: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if not isinstance(config, ClusterConfig):
            config = ClusterConfig.from_spec(config, virtual_nodes=virtual_nodes)
        self._timeout_s = timeout_s
        self._pool_size = pool_size
        self._retries = retries
        self._backoff_s = retry_backoff_s
        self._on_stale = on_stale
        self._wait_replicas = wait_replicas
        self._wait_timeout_ms = round(wait_timeout_s * 1000)
        self._read_from_replicas = read_from_replicas
        self._replica_turn = itertools.count()
        # Which replicas may serve reads (the cluster manager's view); all if unset.
        self.is_healthy: Callable[[str], bool] | None = None
        self._pools: dict[str, KVClientPool] = {}
        self.config = config
        self.ring: ConsistentHashRing = config.ring()
        self.retries_performed = 0
        # Metrics: client commands by name; per group, batches (latency) and commands.
        self.commands: dict[str, int] = {}
        self.group_stats = CommandStats()
        self.group_commands: dict[str, int] = {}

    # ------------------------------------------------------------ config
    def apply_config(self, config: ClusterConfig) -> bool:
        """Switch to ``config`` if it is newer; returns whether it was applied."""
        if config.epoch <= self.config.epoch:
            return False
        old = self.config
        self.config = config
        if config.ring_ids != old.ring_ids or config.virtual_nodes != old.virtual_nodes:
            self.ring = config.ring()
        members = {address for group in config.shards for address in group.members}
        for address in [a for a in self._pools if a not in members]:
            pool = self._pools.pop(address)
            asyncio.get_running_loop().create_task(pool.close())
        logger.info("cluster config applied", extra={"epoch": config.epoch})
        return True

    def owner(self, key: str) -> str:
        """The shard group that owns ``key``."""
        return self.ring.get_node(hash_slot_key(key))

    def primary(self, shard: str) -> str:
        return self.config.group(shard).primary

    def pool(self, address: str) -> KVClientPool:
        pool = self._pools.get(address)
        if pool is None:
            pool = KVClientPool(address, size=self._pool_size, timeout_s=self._timeout_s)
            self._pools[address] = pool
        return pool

    # ---------------------------------------------------------- commands
    async def execute(self, command: str, *args: Any) -> Any:
        (result,) = await self.execute_batch([[command, *args]])
        if isinstance(result, KVStoreError):
            raise result
        return result

    async def execute_batch(self, commands: Sequence[Sequence[Any]]) -> list[Any]:
        """Run a client's pipeline; one result per command, errors in place."""
        results: list[Any] = [None] * len(commands)
        pending: dict[str, list[int]] = {}
        counts = self.commands
        for command in commands:
            name = str(command[0]).lower() if command else ""
            name = name if name.upper() in COMMANDS else "unknown"
            counts[name] = counts.get(name, 0) + 1
        for i, command in enumerate(commands):
            try:
                routed, local = self._route(command)
            except KVStoreError as exc:
                results[i] = exc
                continue
            if routed is None:
                results[i] = local
            elif routed.fanout:
                # A barrier: everything before it is answered first, as a
                # single server would order them.
                await self._flush(commands, pending, results)
                results[i] = await self._fanout(command)
            else:
                assert routed.shard is not None
                pending.setdefault(routed.shard, []).append(i)
        await self._flush(commands, pending, results)
        return results

    def _route(self, command: Sequence[Any]) -> tuple[_Routed | None, Any]:
        name, args = command[0], list(command[1:])
        spec = COMMANDS.get(name.upper()) if isinstance(name, str) else None
        if spec is None:
            raise UnknownCommandError(f"unknown command '{name}'")
        spec.check_arity(args)
        if STATELESS in spec.flags:
            # PING, ECHO, CLIENT, COMMAND...: no keyspace needed.
            return None, spec.handler(cast(CommandContext, None), args)
        keys = spec.keys(args)
        if not keys:
            if spec.name not in FANOUT:
                raise CommandError(f"'{spec.name}' is not supported through the router")
            return _Routed(fanout=True, readonly=not spec.is_write), None
        if not all(isinstance(key, str) for key in keys):
            raise InvalidArgumentError("key must be a string")
        owners = {self.owner(key) for key in keys}
        if len(owners) > 1:
            # Same rule as Redis Cluster: no cross-group atomicity, use hash tags instead.
            raise CrossShardError("Keys in request don't hash to the same shard")
        return _Routed(shard=owners.pop(), readonly=not spec.is_write), None

    async def _flush(
        self, commands: Sequence[Sequence[Any]], pending: dict[str, list[int]], results: list[Any]
    ) -> None:
        if not pending:
            return
        groups = list(pending.items())
        pending.clear()
        replies = await asyncio.gather(
            *(self._send(shard, [commands[i] for i in idxs]) for shard, idxs in groups)
        )
        for (_, idxs), group_replies in zip(groups, replies, strict=True):
            for i, reply in zip(idxs, group_replies, strict=True):
                results[i] = reply

    async def _fanout(self, command: Sequence[Any]) -> Any:
        spec = COMMANDS[str(command[0]).upper()]
        replies = await asyncio.gather(
            *(self._send(group.id, [command]) for group in self.config.shards)
        )
        results = [reply for (reply,) in replies]
        for result in results:
            if isinstance(result, KVStoreError):
                return result
        return FANOUT[spec.name](results)

    async def _send(self, shard: str, commands: list[Sequence[Any]]) -> list[Any]:
        """One group's share of a batch, with the safe retries described above."""
        started = time.perf_counter()
        results = await self._send_with_retries(shard, commands)
        stats = self.group_stats
        stats.record(shard, time.perf_counter() - started)
        self.group_commands[shard] = self.group_commands.get(shard, 0) + len(commands)
        for result in results:
            if isinstance(result, KVStoreError):
                stats.error(shard, result.prefix)
        return results

    async def _send_with_retries(self, shard: str, commands: list[Sequence[Any]]) -> list[Any]:
        results: list[Any] = [None] * len(commands)
        todo = list(range(len(commands)))
        readonly = all(self._is_read(commands[i]) for i in todo)
        # Not while keys are moving: a replica would answer "missing" for a
        # key its primary has already handed over, instead of -ASK.
        replica = readonly and self._read_from_replicas and self.config.rebalance is None
        redirect: dict[int, str] = {}
        for attempt in range(self._retries + 1):
            if attempt:
                self.retries_performed += 1
                await asyncio.sleep(self._backoff_s * 2 ** (attempt - 1))
            by_address: dict[str, list[int]] = {}
            for i in todo:
                # -ASK: the node that now holds the key. Otherwise the current
                # primary of the command's group, re-routed after the first try
                # because a failover or rebalance may have happened meanwhile.
                address = redirect.get(i) or self._address(
                    commands[i], shard if not attempt else None, replica=replica
                )
                if isinstance(address, KVStoreError):
                    results[i] = address
                else:
                    by_address.setdefault(address, []).append(i)
            retry: list[int] = []
            stale = False
            for address, idxs in by_address.items():
                try:
                    replies = await self._exchange(address, [commands[i] for i in idxs])
                except ConnectFailedError as exc:
                    # Never sent: always safe to retry. The primary may have
                    # failed, so ask for a fresher config too.
                    retry += idxs
                    stale = True
                    for i in idxs:
                        results[i] = exc
                    continue
                except NodeUnavailableError as exc:
                    # Sent, outcome unknown: only reads may be repeated.
                    for i in idxs:
                        results[i] = exc
                    if readonly:
                        retry += idxs
                    continue
                for i, reply in zip(idxs, replies, strict=True):
                    results[i] = reply
                    if isinstance(reply, AskRedirectError):
                        redirect[i] = reply.target
                        retry.append(i)
                    elif isinstance(reply, _RETRYABLE):
                        redirect.pop(i, None)
                        retry.append(i)
                        stale = stale or isinstance(reply, ReadOnlyReplicaError)
            if not retry:
                break
            if stale and self._on_stale is not None:
                await self._on_stale()  # ask for a fresher config before retrying
            todo = sorted(retry)
        return results

    async def _exchange(self, address: str, commands: list[Sequence[Any]]) -> list[Any]:
        """One pipeline to one node, with ``WAIT`` appended when writes need replicas."""
        needed = self._wait_replicas
        writes = [i for i, command in enumerate(commands) if not self._is_read(command)]
        if not needed or not writes:
            return await self.pool(address).pipeline(commands)
        *replies, confirmed = await self.pool(address).pipeline(
            [*commands, ["WAIT", needed, self._wait_timeout_ms]]
        )
        if isinstance(confirmed, int) and confirmed >= needed:
            return replies
        # Applied on the primary but not (yet) on enough replicas: report it,
        # don't retry -- repeating a non-idempotent write would apply it twice.
        reason = NotEnoughReplicasError(f"write reached {confirmed} of {needed} replicas")
        return [reason if i in writes and not isinstance(r, KVStoreError) else r
                for i, r in enumerate(replies)]  # fmt: skip

    def _address(
        self, command: Sequence[Any], shard: str | None, *, replica: bool = False
    ) -> str | KVStoreError:
        """Where to send ``command``: ``shard``'s primary (or a replica, for reads).

        Without a shard, the command is routed again (after a failover or rebalance).
        """
        if shard is None:
            try:
                routed, _ = self._route(command)
            except KVStoreError as exc:  # e.g. CROSSSLOT once a rebalance split two keys
                return exc
            assert routed is not None
            assert routed.shard is not None
            shard = routed.shard
        try:
            group = self.config.group(shard)
        except KeyError:
            return NodeUnavailableError(f"shard group {shard!r} left the cluster")
        if replica:
            healthy = [r for r in group.replicas if self.is_healthy is None or self.is_healthy(r)]
            if healthy:
                return healthy[next(self._replica_turn) % len(healthy)]
        return group.primary

    @staticmethod
    def _is_read(command: Sequence[Any]) -> bool:
        spec: CommandSpec | None = COMMANDS.get(str(command[0]).upper())
        return spec is not None and not spec.is_write

    # ------------------------------------------------------------ health
    async def node_status(self) -> list[NodeStatus]:
        checks = [
            self._ping(group.id, address, "primary" if address == group.primary else "replica")
            for group in self.config.shards
            for address in group.members
        ]
        return list(await asyncio.gather(*checks))

    async def close(self) -> None:
        pools, self._pools = list(self._pools.values()), {}
        await asyncio.gather(*(pool.close() for pool in pools))

    async def _ping(self, shard: str, address: str, role: str) -> NodeStatus:
        start = time.perf_counter()
        try:
            await self.pool(address).execute("PING")
        except KVStoreError as exc:
            return NodeStatus(address, shard, role, healthy=False, error=exc.message)
        latency_ms = round((time.perf_counter() - start) * 1000, 3)
        return NodeStatus(address, shard, role, healthy=True, latency_ms=latency_ms)
