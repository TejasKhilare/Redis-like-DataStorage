"""asyncio TCP server for the data plane (shared by shards and the router)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from kvstore.core.exceptions import KVStoreError, ProtocolError
from kvstore.protocol.json_lines import decode_request, encode_error, encode_result

logger = logging.getLogger(__name__)

CommandHandler = Callable[..., Awaitable[Any]]
"""``await handler(command, *args)`` -> result, or raises :class:`KVStoreError`."""

_SHUTDOWN_TIMEOUT_S = 5.0


class TCPServer:
    def __init__(
        self,
        handler: CommandHandler,
        *,
        host: str,
        port: int,
        max_request_bytes: int = 1024 * 1024,
    ) -> None:
        self._handler = handler
        self._host = host
        self._port = port
        self._max_request_bytes = max_request_bytes
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.StreamWriter] = set()

    @property
    def is_serving(self) -> bool:
        return self._server is not None and self._server.is_serving()

    @property
    def port(self) -> int:
        """The bound port (useful when started with port 0)."""
        if self._server is None:
            raise RuntimeError("server is not running")
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._serve_client, self._host, self._port, limit=self._max_request_bytes
        )
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
        logger.debug("client connected", extra={"peer": peer})
        try:
            # Requests are handled in order, so pipelining (sending many
            # requests before reading replies) works without extra code.
            while True:
                try:
                    line = await reader.readline()
                except ValueError:  # the line exceeded the stream limit
                    error = ProtocolError(f"request exceeds {self._max_request_bytes} bytes")
                    writer.write(encode_error(error))
                    await writer.drain()
                    break
                if not line:
                    break
                if line.strip():
                    writer.write(await self._respond(line))
                    await writer.drain()
        except ConnectionError:
            pass
        finally:
            self._connections.discard(writer)
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            logger.debug("client disconnected", extra={"peer": peer})

    async def _respond(self, line: bytes) -> bytes:
        try:
            command, args = decode_request(line)
            result = await self._handler(command, *args)
        except KVStoreError as exc:
            return encode_error(exc)
        except Exception:
            logger.exception("unhandled error while executing a command")
            return encode_error(KVStoreError("internal error"))
        return encode_result(result)
