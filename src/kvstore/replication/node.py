"""A shard node: its engine plus its replication role.

Commands from clients go through :meth:`ShardNode.execute`, which answers
the replication commands itself and passes everything else to the engine:

* ``PSYNC`` / ``REPLCONF``   a replica's handshake (see :mod:`.primary`);
* ``REPLICAOF host port``    become a replica; ``REPLICAOF NO ONE``: become a primary.
  ``... EPOCH <n>`` (kvstore's extension, sent by the cluster manager)
  refuses the change if the node has already seen a newer epoch;
* ``ROLE``, ``WAIT``, and a ``replication`` section in ``INFO``.

A replica refuses writes (``-READONLY``), so a client or router that still
thinks it is the primary is told to look again. With ``min-replicas-to-write``
a primary refuses writes (``-NOREPLICAS``) when too few replicas have
acknowledged recently: a primary cut off from its replicas -- the side of a
partition that will lose the failover -- stops accepting writes that would
be lost, which bounds split-brain damage to the configured lag.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from kvstore.cluster.migration import KeyMigration, MigrationPlan
from kvstore.core.codec import SnapshotRecord
from kvstore.core.exceptions import (
    CommandError,
    InvalidArgumentError,
    KVStoreError,
    ReadOnlyReplicaError,
)
from kvstore.engine import Engine
from kvstore.engine.commands import COMMANDS
from kvstore.observability.metrics import CommandStats
from kvstore.protocol.resp import OK, SimpleString
from kvstore.protocol.tcp_server import Takeover
from kvstore.replication.primary import PrimaryReplication, ReplicationState
from kvstore.replication.replica import ReplicaLink
from kvstore.replication.stream import Backlog

logger = logging.getLogger(__name__)

Role = Literal["primary", "replica"]
_STATE_FILE = "node.json"
# Answered here rather than by the engine (for metric labels).
_NODE_COMMANDS = frozenset({"PSYNC", "REPLCONF", "REPLICAOF", "SLAVEOF", "ROLE", "WAIT", "CLUSTER"})


@dataclass(frozen=True, slots=True)
class ReplicationSettings:
    backlog_bytes: int = 1024 * 1024
    timeout_s: float = 5.0
    ping_interval_s: float = 1.0
    min_replicas_to_write: int = 0
    min_replicas_max_lag_s: float = 10.0


class ShardNode:
    def __init__(
        self,
        engine: Engine,
        *,
        listening_port: int = 0,
        data_dir: Path | None = None,
        settings: ReplicationSettings | None = None,
        metrics: bool = True,
    ) -> None:
        self.engine = engine
        self.settings = settings or ReplicationSettings()
        self.listening_port = listening_port
        self._data_dir = data_dir
        self.state = ReplicationState(Backlog(self.settings.backlog_bytes))
        self.primary = PrimaryReplication(
            self.state, self._begin_snapshot, timeout_s=self.settings.timeout_s
        )
        self.role: Role = "primary"
        self.link: ReplicaLink | None = None
        self.epoch = self._load_epoch()
        self._pinger: asyncio.Task[None] | None = None
        self.migration: KeyMigration | None = None
        self.stats = CommandStats(enabled=metrics)
        engine.replication = self.primary
        engine.extra_info = self._info_sections

    def _begin_snapshot(self) -> asyncio.Future[list[SnapshotRecord]]:
        """The keyspace as of now, for a replica's full resync.

        Copied incrementally when the engine does that (no long pause) and no
        other copy is running; otherwise in one go.
        """
        future: asyncio.Future[list[SnapshotRecord]] = asyncio.get_running_loop().create_future()
        engine = self.engine
        if engine.incremental_snapshots and not engine.snapshot_in_progress:

            def done(records: list[SnapshotRecord]) -> None:
                if not future.done():  # the replica may have gone meanwhile
                    future.set_result(records)

            engine.begin_snapshot(done)
        else:
            future.set_result(engine.store.snapshot())
        return future

    # ------------------------------------------------------------ lifecycle
    def start(self, replicaof: str | None = None) -> None:
        self._pinger = asyncio.get_running_loop().create_task(
            self._ping_replicas(), name="repl-ping"
        )
        if replicaof:
            host, _, port = replicaof.rpartition(":")
            self._become_replica(host, int(port))

    async def stop(self) -> None:
        if self._pinger is not None:
            self._pinger.cancel()
            with suppress(asyncio.CancelledError):
                await self._pinger
        if self.link is not None:
            await self.link.stop()
        if self.migration is not None:
            await self.migration.stop()
        self.primary.disconnect_all()

    async def _ping_replicas(self) -> None:
        while True:
            await asyncio.sleep(self.settings.ping_interval_s)
            if self.role == "primary":
                self.primary.ping()

    # ------------------------------------------------------------- commands
    def execute(self, command: str, *args: Any) -> Any:
        if not self.stats.enabled:
            return self._execute(command, args)
        name = command.upper() if isinstance(command, str) else ""
        label = name.lower() if name in COMMANDS or name in _NODE_COMMANDS else "unknown"
        started = time.perf_counter()
        try:
            return self._execute(command, args)
        except KVStoreError as exc:
            self.stats.error(label, exc.prefix)
            raise
        finally:
            self.stats.record(label, time.perf_counter() - started)

    def _execute(self, command: str, args: tuple[Any, ...]) -> Any:
        name = command.upper() if isinstance(command, str) else ""
        if name == "PSYNC":
            return self._psync(list(args))
        if name == "REPLCONF":
            return self._replconf(list(args))
        if name in ("REPLICAOF", "SLAVEOF"):
            return self._replicaof(list(args))
        if name == "ROLE":
            return self._role()
        if name == "WAIT":
            return self._wait(list(args))
        if name == "CLUSTER":
            return self._cluster(list(args))
        spec = COMMANDS.get(name)
        if spec is not None and spec.is_write:
            if self.role == "replica":
                raise ReadOnlyReplicaError()
            self.primary.check_min_replicas(
                self.settings.min_replicas_to_write, self.settings.min_replicas_max_lag_s
            )
        if self.migration is not None:
            self.migration.check(name, args)  # -ASK / -TRYAGAIN for keys on the move
        return self.engine.execute(command, *args)

    # ------------------------------------------------------------ rebalance
    def _cluster(self, args: list[Any]) -> Any:
        """``CLUSTER REBALANCE <plan> | STATUS | STOP`` -- sent by the cluster manager."""
        if len(args) < 2 or str(args[0]).upper() != "REBALANCE":
            raise CommandError("unknown CLUSTER subcommand (only REBALANCE is supported)")
        sub = str(args[1]).upper()
        if sub == "STATUS":
            m = self.migration
            if m is None:
                return ["idle", 0, ""]
            return [m.state, m.moved, m.error or ""]
        if sub == "STOP":
            if self.migration is not None:
                migration, self.migration = self.migration, None
                asyncio.get_running_loop().create_task(migration.stop())
            return OK
        if self.role != "primary":
            raise CommandError("only a primary moves keys")
        try:
            plan = MigrationPlan.from_args(args[1:])
        except ValueError as exc:
            raise InvalidArgumentError(str(exc)) from None
        if self.migration is not None and self.migration.plan == plan:
            return OK  # the manager retries: already running
        if self.migration is not None:
            raise CommandError("another rebalance is in progress")
        self.migration = KeyMigration(self.engine, plan)
        self.migration.start()
        return OK

    def _psync(self, args: list[Any]) -> Takeover:
        if len(args) != 2:
            raise CommandError("wrong number of arguments for 'psync' command")
        if self.role != "primary":
            raise CommandError("can't PSYNC from a replica (chained replication is not supported)")
        replid, offset = str(args[0]), _int(args[1])

        async def run(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self.primary.serve(reader, writer, replid=replid, offset=offset)

        return Takeover(run)

    @staticmethod
    def _replconf(args: list[Any]) -> SimpleString:
        # Accepted for compatibility. Handlers here don't know which connection
        # a command came from, so the replica announces its port again on its
        # own stream once synced, where the primary can tie it to the link.
        return OK

    def _replicaof(self, args: list[Any]) -> SimpleString:
        epoch: int | None = None
        if len(args) >= 2 and str(args[-2]).upper() == "EPOCH":
            epoch = _int(args[-1])
            args = args[:-2]
            if epoch < self.epoch:
                raise CommandError(f"stale epoch {epoch}: this node is at epoch {self.epoch}")
        if len(args) != 2:
            raise CommandError("wrong number of arguments for 'replicaof' command")
        if str(args[0]).upper() == "NO" and str(args[1]).upper() == "ONE":
            self._become_primary()
        else:
            self._become_replica(str(args[0]), _int(args[1]))
        if epoch is not None and epoch != self.epoch:
            self.epoch = epoch
            self._save_epoch()
        return OK

    def _become_primary(self) -> None:
        if self.role == "primary":
            return
        if self.link is not None:
            # Synchronously: not one more stream command may be applied now.
            self.link.cancel()
            self.link = None
        self.state.fork()  # new history; replicas of the old one can still PSYNC here
        self.primary.active = True
        self.role = "primary"
        self.engine.replication = self.primary
        self.engine.store.expiry_deletes = True
        logger.info("promoted to primary", extra={"replid": self.state.replid})

    def _become_replica(self, host: str, port: int) -> None:
        if self.link is not None:
            if (self.link.host, self.link.port) == (host, port):
                return
            self.link.cancel()
            self.link = None
        self.role = "replica"
        self.primary.disconnect_all()  # no chained replication
        self.engine.replication = None  # the stream carries the primary's effects
        self.engine.store.expiry_deletes = False  # expiries arrive as DELs
        self.link = ReplicaLink(
            self.engine,
            self.state,
            host=host,
            port=port,
            listening_port=self.listening_port,
            timeout_s=self.settings.timeout_s,
        )
        self.link.start()
        logger.info("replicating", extra={"primary": f"{host}:{port}"})

    def _role(self) -> list[Any]:
        if self.role == "primary":
            return [
                "master",
                self.state.offset,
                [[r.host, str(r.port), str(r.ack_offset)] for r in self.primary.replicas.values()],
            ]
        assert self.link is not None
        return ["slave", self.link.host, self.link.port, self.link.link_state, self.state.offset]

    def _wait(self, args: list[Any]) -> int | Awaitable[int]:
        if len(args) != 2:
            raise CommandError("wrong number of arguments for 'wait' command")
        needed, timeout_ms = _int(args[0]), _int(args[1])
        if needed < 0 or timeout_ms < 0:
            raise InvalidArgumentError("timeout is negative")
        if self.role != "primary":
            raise CommandError("WAIT cannot be used with replica instances")
        return self.primary.wait(needed, timeout_ms / 1000)

    # ----------------------------------------------------------------- info
    def _info_sections(self) -> dict[str, dict[str, Any]]:
        state = self.state
        fields: dict[str, Any] = {"role": "master" if self.role == "primary" else "slave"}
        if self.role == "replica" and self.link is not None:
            link = self.link
            fields |= {
                "master_host": link.host,
                "master_port": link.port,
                "master_link_status": "up" if link.up else "down",
                "master_last_io_seconds_ago": int(max(0.0, time.monotonic() - link.last_io)),
                "master_sync_in_progress": link.link_state == "sync",
                "slave_repl_offset": state.offset,
                "slave_read_only": 1,
            }
        replicas = list(self.primary.replicas.values()) if self.role == "primary" else []
        fields["connected_slaves"] = len(replicas)
        for i, r in enumerate(replicas):
            fields[f"slave{i}"] = (
                f"ip={r.host},port={r.port},state={r.state},"
                f"offset={r.ack_offset},lag={self.primary.lag_s(r)}"
            )
        fields |= {
            "master_replid": state.replid,
            "master_replid2": state.replid2,
            "master_repl_offset": state.offset,
            "second_repl_offset": state.second_offset,
            "repl_backlog_active": int(self.primary.active or self.role == "replica"),
            "repl_backlog_size": state.backlog.capacity,
            "repl_backlog_first_byte_offset": state.backlog.start,
            "repl_backlog_histlen": len(state.backlog),
            "min_replicas_to_write": self.settings.min_replicas_to_write,
            "epoch": self.epoch,
        }
        return {"replication": fields}

    # ---------------------------------------------------------------- epoch
    def _load_epoch(self) -> int:
        if self._data_dir is None:
            return 0
        try:
            data = json.loads((self._data_dir / _STATE_FILE).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        return int(data.get("epoch", 0))

    def _save_epoch(self) -> None:
        if self._data_dir is None:
            return
        self._data_dir.mkdir(parents=True, exist_ok=True)
        path = self._data_dir / _STATE_FILE
        tmp = path.with_name(_STATE_FILE + ".tmp")
        tmp.write_text(json.dumps({"epoch": self.epoch}), encoding="utf-8")
        os.replace(tmp, path)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise InvalidArgumentError("value is not an integer or out of range") from None
