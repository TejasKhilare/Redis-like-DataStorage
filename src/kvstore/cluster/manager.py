"""Failure detection and automatic failover (the router's cluster manager).

The manager plays the role of a Redis Sentinel for every shard group:

1. **Heartbeats.** Every ``heartbeat_interval_s`` it asks each node for
   ``INFO replication``. A node that has not answered for ``suspect_after_s``
   is *suspect*; after ``dead_after_s``, *dead*.
2. **Failover.** When a group's primary is dead, it promotes the healthy
   replica with the highest replication offset -- the one that lost the
   fewest writes -- under the next epoch: ``REPLICAOF NO ONE EPOCH <e>``.
   The new configuration is saved and handed to the router at once, and the
   other replicas are pointed at the new primary.
3. **Reconciliation.** Every round, each node is compared with the
   configuration and corrected. This is what fences a primary that comes
   back after being replaced: it still reports ``role:master`` at an older
   epoch, so it is told to replicate the new primary, and its resync --
   full, because its history diverged -- discards the writes it took alone.

Nodes refuse ``REPLICAOF`` from an older epoch, so a stale manager cannot
undo a newer decision. The manager itself is a single process; running it
replicated (Raft) is the stretch goal noted in ADR-0008.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from kvstore.cluster.migration import MigrationPlan
from kvstore.cluster.topology import ClusterConfig, Rebalance, ShardGroup
from kvstore.core.exceptions import ClusterError, KVStoreError
from kvstore.protocol.client import KVClient

logger = logging.getLogger(__name__)

Health = Literal["healthy", "suspect", "dead"]


@dataclass(slots=True)
class NodeHealth:
    address: str
    state: Health = "healthy"
    last_ok: float = field(default_factory=time.monotonic)
    role: str | None = None  # "master" | "slave", as reported
    offset: int = 0
    epoch: int = 0
    replicating: str | None = None  # "host:port" of its primary, if a replica
    link_up: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class FailoverEvent:
    shard: str
    old_primary: str
    new_primary: str
    epoch: int
    reason: str
    detected_after_s: float  # silence of the old primary before promotion
    promoted_at: float  # wall clock
    candidate_offsets: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "shard": self.shard,
            "old_primary": self.old_primary,
            "new_primary": self.new_primary,
            "epoch": self.epoch,
            "reason": self.reason,
            "detected_after_s": round(self.detected_after_s, 3),
            "promoted_at": self.promoted_at,
            "candidate_offsets": self.candidate_offsets,
        }


def parse_info(text: str) -> dict[str, str]:
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and not line.startswith("#"):
            fields[key.strip()] = value.strip()
    return fields


class ClusterManager:
    def __init__(
        self,
        config: ClusterConfig,
        *,
        on_change: Callable[[ClusterConfig], object],
        config_path: Path | None = None,
        heartbeat_interval_s: float = 0.5,
        suspect_after_s: float = 1.0,
        dead_after_s: float = 2.0,
        failover_enabled: bool = True,
    ) -> None:
        self.config = config
        self._on_change = on_change
        self._path = config_path
        self.heartbeat_interval_s = heartbeat_interval_s
        self.suspect_after_s = suspect_after_s
        self.dead_after_s = dead_after_s
        self.failover_enabled = failover_enabled
        self.health: dict[str, NodeHealth] = {}
        self.events: list[FailoverEvent] = []
        self._clients: dict[str, KVClient] = {}
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="cluster-manager")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        await asyncio.gather(*(client.close() for client in self._clients.values()))

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("cluster manager round failed")
            await asyncio.sleep(self.heartbeat_interval_s)

    def set_config(self, config: ClusterConfig) -> None:
        """Adopt ``config`` (a newer epoch): save it, then tell the router."""
        self.config = config
        if self._path is not None:
            config.save(self._path)
        self._on_change(config)

    # --------------------------------------------------------------- rounds
    async def tick(self) -> None:
        """One heartbeat round, then failovers and corrections."""
        async with self._lock:
            members = [a for group in self.config.shards for a in group.members]
            await asyncio.gather(*(self._probe(address) for address in members))
            for group in self.config.shards:
                primary = self.health[group.primary]
                if primary.state == "dead" and self.failover_enabled:
                    await self._failover(group, reason="primary unreachable")
                else:
                    await self._reconcile(self.config.group(group.id))

    async def _probe(self, address: str) -> bool:
        """One heartbeat; returns whether the node answered."""
        health = self.health.setdefault(address, NodeHealth(address))
        client = self._client(address)
        try:
            info = parse_info(await client.execute("INFO", "replication"))
        except KVStoreError as exc:
            silent = time.monotonic() - health.last_ok
            health.error = exc.message
            if silent >= self.dead_after_s:
                if health.state != "dead":
                    logger.warning("node is down", extra={"node": address, "silent_s": silent})
                health.state = "dead"
            elif silent >= self.suspect_after_s:
                health.state = "suspect"
            return False
        if health.state != "healthy":
            logger.info("node is back", extra={"node": address})
        health.state, health.last_ok, health.error = "healthy", time.monotonic(), None
        health.role = info.get("role")
        health.epoch = int(info.get("epoch", 0))
        if health.role == "slave":
            health.offset = int(info.get("slave_repl_offset", 0))
            health.replicating = f"{info.get('master_host')}:{info.get('master_port')}"
            health.link_up = info.get("master_link_status") == "up"
        else:
            health.offset = int(info.get("master_repl_offset", 0))
            health.replicating, health.link_up = None, False
        return True

    def is_healthy(self, address: str) -> bool:
        """Answering, and if a replica, linked to its primary (for replica reads)."""
        health = self.health.get(address)
        if health is None or health.state != "healthy":
            return False
        return health.role != "slave" or health.link_up

    def _client(self, address: str) -> KVClient:
        client = self._clients.get(address)
        if client is None:
            # A reply slower than the suspect threshold counts as no reply (a hung
            # node is as bad as a dead one) -- but no sooner: tied to the heartbeat
            # interval, a 100 ms latency spike made a healthy primary look dead
            # and triggered a failover (found by tests/integration/test_chaos.py).
            timeout = max(0.05, self.suspect_after_s)
            client = KVClient.from_address(address, timeout_s=timeout)
            self._clients[address] = client
        return client

    # ------------------------------------------------------------- failover
    async def failover(self, shard: str, *, reason: str = "requested") -> FailoverEvent:
        """Promote the best replica of ``shard`` now (``POST .../failover``)."""
        async with self._lock:
            group = self.config.group(shard)
            event = await self._failover(group, reason=reason, graceful=True)
            if event is None:
                raise ClusterError(f"no healthy replica to promote in {shard!r}")
            return event

    async def _failover(
        self, group: ShardGroup, *, reason: str, graceful: bool = False
    ) -> FailoverEvent | None:
        candidates = [
            self.health[r]
            for r in group.replicas
            if r in self.health and self.health[r].state == "healthy"
            and self.health[r].role == "slave"
        ]  # fmt: skip
        if not candidates:
            logger.error("no replica to promote", extra={"shard": group.id})
            return None
        if graceful:
            await self._let_replicas_catch_up(group, candidates)
        winner = max(candidates, key=lambda h: h.offset)
        epoch = self.config.epoch + 1
        try:
            await self._client(winner.address).execute("REPLICAOF", "NO", "ONE", "EPOCH", epoch)
        except KVStoreError as exc:
            logger.error("promotion failed", extra={"node": winner.address, "error": exc.message})
            return None
        old = self.health.get(group.primary)
        replicas = tuple(a for a in group.members if a != winner.address)
        self.set_config(self.config.with_group(ShardGroup(group.id, winner.address, replicas)))
        event = FailoverEvent(
            shard=group.id,
            old_primary=group.primary,
            new_primary=winner.address,
            epoch=epoch,
            reason=reason,
            detected_after_s=time.monotonic() - old.last_ok if old else 0.0,
            promoted_at=time.time(),
            candidate_offsets={h.address: h.offset for h in candidates},
        )
        self.events.append(event)
        winner.role = "master"
        logger.warning("failover", extra=event.to_dict())
        await self._reconcile(self.config.group(group.id))
        return event

    async def _let_replicas_catch_up(
        self, group: ShardGroup, candidates: list[NodeHealth], timeout_s: float = 1.0
    ) -> None:
        """For a requested failover: wait (briefly) until a replica has every write."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.gather(*(self._probe(a) for a in group.members))
            primary = self.health[group.primary]
            if any(c.offset >= primary.offset for c in candidates):
                return
            await asyncio.sleep(0.02)

    # ------------------------------------------------------------ rebalance
    async def add_group(self, group: ShardGroup) -> dict[str, Any]:
        """Add a shard group (it should be empty) and move its share of keys to it."""
        if group.id in {g.id for g in self.config.shards}:
            raise ClusterError(f"shard group {group.id!r} already exists")
        # A node never seen before starts out "healthy", so require an answer now.
        if not await self._probe(group.primary):
            raise ClusterError(f"{group.primary} is not reachable")
        async with self._lock:
            self.set_config(self.config.next_epoch(shards=(*self.config.shards, group)))
        return await self._rebalance((*self.config.ring_ids, group.id))

    async def remove_group(self, shard: str) -> dict[str, Any]:
        """Move every key off ``shard``, then drop it from the cluster."""
        self.config.group(shard)  # KeyError if unknown
        remaining = tuple(s for s in self.config.ring_ids if s != shard)
        if not remaining:
            raise ClusterError("cannot remove the last shard group")
        return await self._rebalance(remaining)

    async def _rebalance(self, target: tuple[str, ...]) -> dict[str, Any]:
        """Route by the old ring while keys move, then switch to ``target``."""
        if self.config.rebalance is not None:
            raise ClusterError("a rebalance is already in progress")
        started = time.monotonic()
        async with self._lock:
            self.set_config(self.config.next_epoch(rebalance=Rebalance(target)))
        sources = self.config.ring_ids
        sent: dict[str, tuple[str, MigrationPlan]] = {}
        base: dict[str, int] = dict.fromkeys(sources, 0)
        moved: dict[str, int] = dict.fromkeys(sources, 0)
        while True:
            finished = 0
            for shard in sources:
                primary = self.config.group(shard).primary
                plan = MigrationPlan(
                    shard,
                    self.config.virtual_nodes,
                    {t: self.config.group(t).primary for t in target},
                )
                client = self._client(primary)
                try:
                    state, count, error = await client.execute("CLUSTER", "REBALANCE", "STATUS")
                    if sent.get(shard) != (primary, plan):
                        # First time, or a failover changed a source or target:
                        # (re)start. Moving is idempotent, so starting over is safe.
                        if state != "idle":
                            await client.execute("CLUSTER", "REBALANCE", "STOP")
                            base[shard] += int(count)
                        await client.execute("CLUSTER", "REBALANCE", *plan.to_args())
                        sent[shard] = (primary, plan)
                        continue
                except KVStoreError as exc:
                    logger.warning(
                        "rebalance poll failed", extra={"node": primary, "error": exc.message}
                    )
                    continue
                if state == "failed":
                    raise ClusterError(f"moving keys off {shard!r} failed: {error}")
                moved[shard] = base[shard] + int(count)
                finished += state == "done"
            if finished == len(sources):
                break
            await asyncio.sleep(0.05)
        # Switch the ring first, then stop the redirects: the other way round,
        # a moved key would briefly be answered as missing by its old owner.
        async with self._lock:
            kept = tuple(g for g in self.config.shards if g.id in target)
            self.set_config(self.config.next_epoch(shards=kept, ring_ids=target, rebalance=None))
        for shard in sources:
            group = next((g for g in kept if g.id == shard), None)
            address = group.primary if group else sent[shard][0]
            await self._command(address, "CLUSTER", "REBALANCE", "STOP")
        total = sum(moved.values())
        logger.info("rebalance done", extra={"moved": total, "ring": list(target)})
        return {
            "epoch": self.config.epoch,
            "ring": list(target),
            "moved_keys": total,
            "moved_by_shard": moved,
            "duration_s": round(time.monotonic() - started, 3),
        }

    # ------------------------------------------------------- reconciliation
    async def _reconcile(self, group: ShardGroup) -> None:
        epoch = self.config.epoch
        primary = self.health.get(group.primary)
        if primary is not None and primary.state == "healthy" and primary.role == "slave":
            await self._command(group.primary, "REPLICAOF", "NO", "ONE", "EPOCH", epoch)
        host, _, port = group.primary.rpartition(":")
        for address in group.replicas:
            health = self.health.get(address)
            if health is None or health.state != "healthy":
                continue
            if health.role != "slave" or health.replicating != group.primary:
                # Includes a replaced primary that came back still acting as one.
                await self._command(address, "REPLICAOF", host, port, "EPOCH", epoch)

    async def _command(self, address: str, *args: Any) -> None:
        try:
            await self._client(address).execute(*args)
        except KVStoreError as exc:
            logger.warning(
                "reconfiguring node failed", extra={"node": address, "error": exc.message}
            )
        else:
            logger.info(
                "node reconfigured", extra={"node": address, "command": " ".join(map(str, args))}
            )
