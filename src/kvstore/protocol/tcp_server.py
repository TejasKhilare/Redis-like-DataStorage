"""asyncio RESP server for the data plane (shared by shards and the router)."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext, suppress
from typing import Any

from kvstore.core.exceptions import KVStoreError, ProtocolError
from kvstore.protocol.resp import OK, RequestParser, encode_error, encode_reply

logger = logging.getLogger(__name__)

CommandHandler = Callable[..., Any]
"""``handler(command, *args)`` -> result (or an awaitable of it); raises :class:`KVStoreError`."""

BatchContext = Callable[[], AbstractContextManager[None]]

_READ_SIZE = 64 * 1024
_SHUTDOWN_TIMEOUT_S = 5.0


class TCPServer:
    """Serves RESP over TCP.

    Every read may carry many pipelined commands. They are all executed
    inside one ``batch()`` context before any reply is written, which lets a
    shard commit the AOF once for the whole batch (group commit): one fsync
    amortized over N writes, and still no reply before its write is durable.

    A synchronous handler (the shard's ``engine.execute``) runs the batch
    without yielding to the event loop, so no other client's commands can
    interleave with it. An async handler (the router's) may await.
    """

    def __init__(
        self,
        handler: CommandHandler,
        *,
        host: str,
        port: int,
        max_request_bytes: int = 64 * 1024 * 1024,
        batch: BatchContext | None = None,
    ) -> None:
        self._handler = handler
        self._host = host
        self._port = port
        self._max_request_bytes = max_request_bytes
        self._batch: BatchContext = batch or nullcontext
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.StreamWriter] = set()
        self.connections_received = 0

    @property
    def is_serving(self) -> bool:
        return self._server is not None and self._server.is_serving()

    @property
    def connected_clients(self) -> int:
        return len(self._connections)

    @property
    def port(self) -> int:
        """The bound port (useful when started with port 0)."""
        if self._server is None:
            raise RuntimeError("server is not running")
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve_client, self._host, self._port)
        logger.info("tcp server listening", extra={"host": self._host, "port": self.port})

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        # Since Python 3.12 wait_closed() also waits for open connections,
        # so close them ourselves instead of hanging on idle clients.
        for writer in list(self._connections):
            writer.close()
        with suppress(TimeoutError):
            await asyncio.wait_for(self._server.wait_closed(), _SHUTDOWN_TIMEOUT_S)
        self._server = None

    async def _serve_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = str(writer.get_extra_info("peername"))
        self._connections.add(writer)
        self.connections_received += 1
        parser = RequestParser(max_bulk=self._max_request_bytes)
        logger.debug("client connected", extra={"peer": peer})
        try:
            while True:
                data = await reader.read(_READ_SIZE)
                if not data:
                    break
                parser.feed(data)
                replies, close = await self._run_batch(parser)
                if replies:
                    writer.write(b"".join(replies))
                    await writer.drain()
                if close:
                    break
        except ConnectionError:
            pass
        except KVStoreError as exc:
            # The batch's group commit failed: its writes are not durable, so
            # no reply may claim success. Drop the connection instead.
            logger.error("dropping connection after failed commit", extra={"error": str(exc)})
        finally:
            self._connections.discard(writer)
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            logger.debug("client disconnected", extra={"peer": peer})

    async def _run_batch(self, parser: RequestParser) -> tuple[list[bytes], bool]:
        replies: list[bytes] = []
        with self._batch():
            while True:
                try:
                    command = parser.next_command()
                except ProtocolError as exc:
                    replies.append(encode_error(exc))
                    return replies, True  # the stream is out of sync: hang up, like Redis
                if command is None:
                    return replies, False
                if not command:
                    continue
                if command[0].upper() == "QUIT":
                    replies.append(encode_reply(OK))
                    return replies, True
                replies.append(await self._respond(command))

    async def _respond(self, command: list[str]) -> bytes:
        try:
            result = self._handler(*command)
            if inspect.isawaitable(result):
                result = await result
            return encode_reply(result)
        except KVStoreError as exc:
            return encode_error(exc)
        except Exception:
            logger.exception("unhandled error while executing a command")
            return encode_error(KVStoreError("internal error"))
