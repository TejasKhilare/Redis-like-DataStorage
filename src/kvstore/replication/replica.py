"""The replica's side of replication: one link to the primary, kept alive.

The link connects, announces its port, asks to continue from where it is
(``PSYNC <replid> <offset>``, or ``PSYNC ? -1`` the first time) and then
applies the stream. It keeps the raw stream bytes in its own backlog under
the same offsets, so that once promoted it can offer partial resyncs to the
other replicas. It acknowledges its offset every second and whenever the
primary asks (``REPLCONF GETACK``). Any error drops the link, which then
reconnects and resyncs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Literal

from kvstore.core.exceptions import KVStoreError, ProtocolError
from kvstore.engine import Engine
from kvstore.engine.persistence.snapshot import decode_snapshot
from kvstore.protocol.resp import encode_command
from kvstore.replication.primary import ReplicationState
from kvstore.replication.stream import next_command

logger = logging.getLogger(__name__)

LinkState = Literal["connect", "sync", "connected"]


class ReplicaLink:
    def __init__(
        self,
        engine: Engine,
        state: ReplicationState,
        *,
        host: str,
        port: int,
        listening_port: int,
        timeout_s: float = 5.0,
        ack_interval_s: float = 1.0,
        retry_s: float = 0.25,
        on_synced: Callable[[], None] | None = None,
    ) -> None:
        self.engine = engine
        self.state = state
        self.host = host
        self.port = port
        self._listening_port = listening_port
        self._timeout_s = timeout_s
        self._ack_interval_s = ack_interval_s
        self._retry_s = retry_s
        self._on_synced = on_synced
        self.link_state: LinkState = "connect"
        self.last_io = time.monotonic()
        self.full_syncs = 0
        self.partial_syncs = 0
        self._task: asyncio.Task[None] | None = None

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def up(self) -> bool:
        return self.link_state == "connected"

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run(), name="replica-link")

    def cancel(self) -> None:
        """Stop at once: the link applies nothing more after this returns."""
        if self._task is not None:
            self._task.cancel()
        self.link_state = "connect"

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.link_state = "connect"

    async def _run(self) -> None:
        while True:
            try:
                await self._session()
            except (OSError, TimeoutError, ProtocolError, EOFError, ValueError) as exc:
                if self.link_state != "connect":
                    logger.warning("replication link lost", extra={"error": repr(exc)})
            except KVStoreError as exc:
                logger.error("replication link failed", extra={"error": exc.message})
            self.link_state = "connect"
            await asyncio.sleep(self._retry_s)

    async def _session(self) -> None:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self._timeout_s
        )
        try:
            self.link_state = "sync"
            await self._handshake(reader, writer)
            self.link_state = "connected"
            self.last_io = time.monotonic()
            if self._on_synced is not None:
                self._on_synced()
            # Announced here, on this link, so the primary knows which replica it is.
            writer.write(encode_command(["REPLCONF", "listening-port", self._listening_port]))
            acker = asyncio.create_task(self._ack_periodically(writer))
            try:
                await self._stream(reader, writer)
            finally:
                acker.cancel()
        finally:
            writer.close()

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(encode_command(["REPLCONF", "listening-port", self._listening_port]))
        await writer.drain()
        await self._expect_ok(reader)
        state = self.state
        known = state.offset > 0 or state.replid2 != "0" * 40
        replid, offset = (state.replid, state.offset) if known else ("?", -1)
        writer.write(encode_command(["PSYNC", replid, offset]))
        await writer.drain()
        line = (await asyncio.wait_for(reader.readline(), self._timeout_s)).decode().strip()
        if line.startswith("+FULLRESYNC"):
            _, new_replid, start = line.split()
            header = await asyncio.wait_for(reader.readline(), self._timeout_s)
            if not header.startswith(b"$"):
                raise ProtocolError(f"expected the snapshot, got {header[:20]!r}")
            size = int(header[1:])
            payload = await asyncio.wait_for(reader.readexactly(size), max(self._timeout_s, 30))
            keys = self.engine.load_snapshot(decode_snapshot(payload, source="full resync"))
            state.backlog.reset(int(start))
            state.replid, state.replid2, state.second_offset = new_replid, "0" * 40, -1
            self.full_syncs += 1
            logger.info("full resync done", extra={"keys": keys, "offset": int(start)})
        elif line.startswith("+CONTINUE"):
            parts = line.split()
            if len(parts) > 1 and parts[1] != state.replid:
                # The primary was promoted and started a new history.
                state.replid2, state.second_offset = state.replid, state.offset
                state.replid = parts[1]
            self.partial_syncs += 1
            logger.info("partial resync", extra={"offset": state.offset})
        else:
            raise ProtocolError(f"PSYNC refused: {line}")

    async def _expect_ok(self, reader: asyncio.StreamReader) -> None:
        line = await asyncio.wait_for(reader.readline(), self._timeout_s)
        if not line.startswith(b"+OK"):
            raise ProtocolError(f"handshake refused: {line.decode(errors='replace').strip()}")

    async def _stream(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        buf, pos = bytearray(), 0
        while True:
            data = await asyncio.wait_for(reader.read(64 * 1024), self._timeout_s)
            if not data:
                raise EOFError("primary closed the link")
            self.last_io = time.monotonic()
            buf += data
            while (parsed := next_command(buf, pos)) is not None:
                args, end = parsed
                self._apply(args, writer)
                # Offsets count applied bytes: a command half-received isn't counted.
                self.state.backlog.append(bytes(buf[pos:end]))
                pos = end
            del buf[:pos]
            pos = 0

    def _apply(self, args: list[str], writer: asyncio.StreamWriter) -> None:
        name = args[0].upper()
        if name == "PING":
            return
        if name == "REPLCONF":
            if len(args) > 1 and args[1].upper() == "GETACK":
                # Acknowledge the offset *before* this request, as Redis does.
                writer.write(encode_command(["REPLCONF", "ACK", self.state.offset]))
            return
        try:
            self.engine.apply_replicated(args[0], args[1:])
        except KVStoreError as exc:
            # The primary ran it successfully, so this means divergence.
            logger.error("replicated command failed", extra={"command": name, "error": exc.message})

    async def _ack_periodically(self, writer: asyncio.StreamWriter) -> None:
        while True:
            writer.write(encode_command(["REPLCONF", "ACK", self.state.offset]))
            await asyncio.sleep(self._ack_interval_s)
