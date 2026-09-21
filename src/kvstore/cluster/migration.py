"""Moving keys to their new owners during a rebalance (runs on each source primary).

The manager tells every primary the ring being moved to. Each one scans its
keys and moves those whose owner changes, in batches: ``DUMP`` locally,
``RESTORE`` on the new owner, then ``DEL`` locally -- the DEL also reaches
this node's AOF and replicas, the RESTORE the target's.

While a migration runs, commands are checked before they reach the engine
(:meth:`KeyMigration.check`), the way Redis Cluster handles a slot being
migrated:

* a key still here is served here -- it will be moved later, with any change;
* a key in a batch on its way out answers ``-TRYAGAIN`` (the copy is in
  flight; a write now would be lost);
* a key that is not here and belongs elsewhere answers ``-ASK <shard> <addr>``:
  it was moved already, or it is new and should be created at its new owner.

Only keys whose owner changes move -- with consistent hashing, about 1/N of
them when a group is added to N.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from kvstore.cluster.hash_ring import ConsistentHashRing, hash_slot_key
from kvstore.core.codec import to_str
from kvstore.core.exceptions import AskRedirectError, KVStoreError, TryAgainError
from kvstore.engine import Engine
from kvstore.engine.commands import COMMANDS
from kvstore.engine.persistence.snapshot import dump_value
from kvstore.protocol.client import KVClient

logger = logging.getLogger(__name__)

State = Literal["running", "done", "failed"]


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """What a source needs: its own group id and the target ring's groups."""

    shard: str
    virtual_nodes: int
    targets: dict[str, str]  # group id -> primary address, for every group in the target ring

    def ring(self) -> ConsistentHashRing:
        return ConsistentHashRing(self.targets, virtual_nodes=self.virtual_nodes)

    @classmethod
    def from_args(cls, args: Sequence[Any]) -> MigrationPlan:
        """``<shard> <virtual-nodes> <id> <address> [<id> <address> ...]``"""
        if len(args) < 4 or len(args) % 2:
            raise ValueError("expected: <shard> <virtual-nodes> <id> <address> [...]")
        pairs = args[2:]
        return cls(
            shard=str(args[0]),
            virtual_nodes=int(args[1]),
            targets={str(pairs[i]): str(pairs[i + 1]) for i in range(0, len(pairs), 2)},
        )

    def to_args(self) -> list[Any]:
        return [self.shard, self.virtual_nodes, *(x for kv in self.targets.items() for x in kv)]


class KeyMigration:
    def __init__(
        self,
        engine: Engine,
        plan: MigrationPlan,
        *,
        batch_size: int = 100,
        timeout_s: float = 5.0,
        retry_s: float = 0.2,
    ) -> None:
        self.engine = engine
        self.plan = plan
        self._ring = plan.ring()
        self._batch_size = batch_size
        self._timeout_s = timeout_s
        self._retry_s = retry_s
        self._in_flight: set[str] = set()
        self._clients: dict[str, KVClient] = {}
        self._task: asyncio.Task[None] | None = None
        self.state: State = "running"
        self.moved = 0
        self.error: str | None = None

    # ------------------------------------------------------------ routing
    def destination(self, key: str) -> tuple[str, str] | None:
        """``(group, address)`` if ``key`` belongs elsewhere once the move is done."""
        shard = self._ring.get_node(hash_slot_key(key))
        if shard == self.plan.shard:
            return None
        return shard, self.plan.targets[shard]

    def check(self, command: str, args: Sequence[Any]) -> None:
        """Raise ``TRYAGAIN`` or ``ASK`` if the command must not run here now."""
        spec = COMMANDS.get(command.upper())
        if spec is None:
            return
        keys = [k for k in spec.keys(args) if isinstance(k, str)]
        if not keys:
            return
        if any(k in self._in_flight for k in keys):
            raise TryAgainError("key is being migrated")
        store = self.engine.store
        gone = [k for k in keys if store.peek(k) is None and self.destination(k) is not None]
        if not gone:
            return
        if len(gone) < len(keys):
            # Some keys here, some already moved: neither node can run it whole.
            raise TryAgainError("multiple keys request during a migration")
        destination = self.destination(gone[0])
        assert destination is not None
        raise AskRedirectError(" ".join(destination))

    # ------------------------------------------------------------ moving
    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run(), name="key-migration")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        await asyncio.gather(*(c.close() for c in self._clients.values()))

    async def _run(self) -> None:
        try:
            # One pass over the keys present now is enough: while migrating, a
            # key that belongs elsewhere is only ever created at its new owner.
            pending = [k for k in list(self.engine.store.iter_keys()) if self.destination(k)]
            while pending:
                batch, pending = pending[: self._batch_size], pending[self._batch_size :]
                await self._move(batch)
            self.state = "done"
            logger.info("migration done", extra={"shard": self.plan.shard, "moved": self.moved})
        except Exception as exc:
            self.state, self.error = "failed", repr(exc)
            logger.exception("migration failed", extra={"shard": self.plan.shard})

    async def _move(self, keys: list[str]) -> None:
        by_target: dict[str, list[tuple[str, list[Any]]]] = {}
        store = self.engine.store
        for key in keys:
            record = store.record(key)  # it may have been deleted or expired meanwhile
            destination = self.destination(key)
            if record is None or destination is None:
                continue
            _, kind, payload, expires_at = record
            ttl_ms = 0 if expires_at is None else max(1, round(expires_at * 1000))
            restore = ["RESTORE", key, ttl_ms, to_str(dump_value(kind, payload)), "REPLACE"]
            if expires_at is not None:
                restore.append("ABSTTL")
            by_target.setdefault(destination[1], []).append((key, restore))
            self._in_flight.add(key)
        try:
            await asyncio.gather(
                *(self._send(address, items) for address, items in by_target.items())
            )
        finally:
            self._in_flight.difference_update(keys)

    async def _send(self, address: str, items: list[tuple[str, list[Any]]]) -> None:
        client = self._client(address)
        while True:
            try:
                replies = await client.pipeline([command for _, command in items])
                break
            except KVStoreError as exc:  # the target is briefly unreachable: keep trying
                logger.warning(
                    "migration batch failed", extra={"to": address, "error": exc.message}
                )
                await asyncio.sleep(self._retry_s)
        moved = []
        for (key, _), reply in zip(items, replies, strict=True):
            if isinstance(reply, KVStoreError):
                raise reply  # e.g. OOM on the target: stop rather than lose the key
            moved.append(key)
        if moved:
            # The copies are safe at the target: remove ours (AOF + replicas too).
            self.engine.execute("DEL", *moved)
            self.moved += len(moved)

    def _client(self, address: str) -> KVClient:
        client = self._clients.get(address)
        if client is None:
            client = self._clients[address] = KVClient.from_address(
                address, timeout_s=self._timeout_s
            )
        return client
