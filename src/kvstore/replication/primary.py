"""The primary's side of replication: resyncs, streaming, acknowledgements.

A replica connects like a client and sends ``PSYNC <replid> <offset>``:

* if the primary still holds that history from that offset (its current
  replication id, or the one it inherited when it was promoted, and the
  offset is inside the backlog), it answers ``+CONTINUE`` and streams the
  missing bytes: a *partial* resync;
* otherwise ``+FULLRESYNC <replid> <offset>``, then a snapshot of the
  keyspace taken at that offset (``$<len>`` + the binary snapshot), then
  the stream from that offset on.

Writes are fed into the backlog as they happen and sent to the replicas at
each commit -- after the AOF, so no replica ever holds a write its primary
could lose on restart. Replicas acknowledge their offset every second
(``REPLCONF ACK``), which gives the lag, ``WAIT`` and ``min-replicas-to-write``.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal

from kvstore.core.codec import SnapshotRecord
from kvstore.core.exceptions import NotEnoughReplicasError
from kvstore.engine.persistence.snapshot import encode_snapshot
from kvstore.protocol.resp import encode_command
from kvstore.replication.stream import Backlog, new_replid, next_command

logger = logging.getLogger(__name__)

# A replica whose unsent output grows past this is dropped (it resyncs later),
# like Redis's client-output-buffer-limit for replicas.
_MAX_REPLICA_BUFFER = 64 * 1024 * 1024


@dataclass
class ReplicationState:
    """This node's position in the stream history (both roles keep one)."""

    backlog: Backlog
    replid: str = field(default_factory=new_replid)
    replid2: str = "0" * 40  # the history this node continued from when promoted
    second_offset: int = -1  # ... valid up to this offset

    @property
    def offset(self) -> int:
        return self.backlog.end

    def can_continue(self, replid: str, offset: int) -> bool:
        same_history = replid == self.replid or (
            replid == self.replid2 and offset <= self.second_offset
        )
        return same_history and self.backlog.contains(offset)

    def fork(self) -> None:
        """Start a new history here (on promotion), remembering the old one for PSYNC."""
        self.replid2, self.second_offset = self.replid, self.offset
        self.replid = new_replid()


@dataclass(eq=False)
class ConnectedReplica:
    id: int
    host: str
    port: int
    writer: asyncio.StreamWriter
    state: Literal["sync", "online"]
    sent_offset: int
    ack_offset: int = 0
    ack_time: float = field(default_factory=time.monotonic)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(eq=False)
class _Waiter:
    offset: int
    needed: int
    future: asyncio.Future[int]


class PrimaryReplication:
    """Feeds the stream, serves PSYNC, tracks replicas. The engine's ``replication`` hook."""

    def __init__(
        self,
        state: ReplicationState,
        snapshot: Callable[[], list[SnapshotRecord]],
        *,
        timeout_s: float = 5.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state = state
        self._snapshot = snapshot
        self._timeout_s = timeout_s
        self._clock = clock
        self.replicas: dict[int, ConnectedReplica] = {}
        self._ids = itertools.count(1)
        self._waiters: list[_Waiter] = []
        # The backlog only exists once a replica has asked for it, so a
        # standalone node pays nothing per write (as in Redis).
        self.active = False
        self.full_resyncs = 0
        self.partial_resyncs = 0

    # ----------------------------------------------------------- feeding
    def feed(self, command: str, args: Sequence[Any]) -> None:
        if self.active:
            self.state.backlog.append(encode_command([command, *args]))

    def flush(self) -> None:
        for replica in list(self.replicas.values()):
            if replica.state == "online":
                self._send(replica)

    def ping(self) -> None:
        """Keep idle links alive (it counts in the stream, as in Redis)."""
        if self.replicas:
            self.feed("PING", ())
            self.flush()

    def _send(self, replica: ConnectedReplica) -> None:
        data = self.state.backlog.since(replica.sent_offset)
        transport = replica.writer.transport
        if data is None or transport.get_write_buffer_size() > _MAX_REPLICA_BUFFER:
            logger.warning("replica fell behind the backlog", extra={"replica": replica.address})
            self._drop(replica)
            return
        if data:
            replica.writer.write(data)
            replica.sent_offset = self.state.offset

    def _drop(self, replica: ConnectedReplica) -> None:
        self.replicas.pop(replica.id, None)
        replica.writer.close()

    def disconnect_all(self) -> None:
        for replica in list(self.replicas.values()):
            self._drop(replica)

    # ------------------------------------------------------------- PSYNC
    async def serve(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        replid: str,
        offset: int,
    ) -> None:
        """Run one replica connection until it closes (the PSYNC takeover)."""
        host = str((writer.get_extra_info("peername") or ("?", 0))[0])
        # Snapshot and offset must be captured together, with no await in
        # between: the snapshot then holds exactly the stream up to `start`.
        full = not self.state.can_continue(replid, offset)
        if full:
            self.active = True
            records = self._snapshot()
            start = self.state.offset
        else:
            start = offset
        replica = ConnectedReplica(next(self._ids), host, 0, writer, "sync", sent_offset=start)
        self.replicas[replica.id] = replica
        try:
            if full:
                self.full_resyncs += 1
                writer.write(b"+FULLRESYNC %s %d\r\n" % (self.state.replid.encode(), start))
                payload = await asyncio.to_thread(
                    encode_snapshot, records, created_at=self._clock()
                )
                writer.write(b"$%d\r\n" % len(payload))
                writer.write(payload)
                logger.info(
                    "full resync",
                    extra={"replica": replica.address, "keys": len(records), "offset": start},
                )
            else:
                self.partial_resyncs += 1
                writer.write(b"+CONTINUE %s\r\n" % self.state.replid.encode())
                logger.info("partial resync", extra={"replica": replica.address, "from": offset})
            await writer.drain()
            if replica.id not in self.replicas:  # dropped while the snapshot was sent
                return
            replica.state = "online"
            replica.ack_offset = start
            self._send(replica)  # what was written while the snapshot was on its way
            await self._read_acks(reader, replica)
        except (ConnectionError, TimeoutError, OSError):
            pass
        finally:
            self._drop(replica)
            logger.info("replica disconnected", extra={"replica": replica.address})

    async def _read_acks(self, reader: asyncio.StreamReader, replica: ConnectedReplica) -> None:
        buf, pos = bytearray(), 0
        while replica.id in self.replicas:
            data = await asyncio.wait_for(reader.read(4096), self._timeout_s)
            if not data:
                return
            buf += data
            while (parsed := next_command(buf, pos)) is not None:
                args, pos = parsed
                if len(args) < 3 or args[0].upper() != "REPLCONF":
                    continue
                if args[1].upper() == "ACK":
                    replica.ack_offset = int(args[2])
                    replica.ack_time = time.monotonic()
                    self._wake_waiters()
                elif args[1].lower() == "listening-port":
                    replica.port = int(args[2])
            del buf[:pos]
            pos = 0

    # ------------------------------------------------ WAIT / min-replicas
    def acked(self, offset: int) -> int:
        return sum(1 for r in self.replicas.values() if r.ack_offset >= offset)

    async def wait(self, needed: int, timeout_s: float) -> int:
        """``WAIT``: block until ``needed`` replicas acknowledged every write so far."""
        target = self.state.offset
        if self.acked(target) >= needed or not self.replicas:
            return self.acked(target)
        future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        waiter = _Waiter(target, needed, future)
        self._waiters.append(waiter)
        self.feed("REPLCONF", ("GETACK", "*"))  # ask for acks now, not in up to a second
        self.flush()
        try:
            return await asyncio.wait_for(future, timeout_s) if timeout_s > 0 else await future
        except TimeoutError:
            return self.acked(target)
        finally:
            with suppress(ValueError):
                self._waiters.remove(waiter)

    def _wake_waiters(self) -> None:
        for waiter in self._waiters:
            count = self.acked(waiter.offset)
            if count >= waiter.needed and not waiter.future.done():
                waiter.future.set_result(count)

    def check_min_replicas(self, needed: int, max_lag_s: float) -> None:
        if needed <= 0:
            return
        now = time.monotonic()
        good = sum(
            1
            for r in self.replicas.values()
            if r.state == "online" and now - r.ack_time <= max_lag_s
        )
        if good < needed:
            raise NotEnoughReplicasError()

    def lag_s(self, replica: ConnectedReplica) -> int:
        return int(time.monotonic() - replica.ack_time)
