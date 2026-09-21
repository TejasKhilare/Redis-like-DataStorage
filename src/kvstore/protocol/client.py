"""Async RESP client (used by the router, the CLI and tests).

One TCP connection carries any number of concurrent requests. RESP answers
requests in order, so each request is a future in a FIFO queue, resolved as
its reply arrives. Requests issued during the same event-loop iteration are
written with a single ``write()`` (automatic pipelining, as ioredis and
Lettuce do): under load, many callers share each round trip instead of
queueing behind a lock for one each.
"""

from __future__ import annotations

import asyncio
import itertools
from collections import deque
from collections.abc import Sequence
from typing import Any, Self, cast

from kvstore.core.exceptions import (
    ConnectFailedError,
    KVStoreError,
    NodeUnavailableError,
    ProtocolError,
)
from kvstore.protocol.resp import NOT_READY, ReplyParser, encode_command


class _Connection(asyncio.Protocol):
    """One socket: queued requests, coalesced writes, replies matched in order."""

    def __init__(self) -> None:
        self._transport: asyncio.Transport | None = None
        self._parser = ReplyParser()
        self._waiting: deque[asyncio.Future[Any]] = deque()
        self._out: list[bytes] = []
        self._flush_scheduled = False
        self.error: Exception | None = None

    @property
    def closed(self) -> bool:
        return self.error is not None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # uvloop's transports implement the interface without subclassing it.
        self._transport = cast(asyncio.Transport, transport)

    def data_received(self, data: bytes) -> None:
        self._parser.feed(data)
        try:
            while True:
                reply = self._parser.next_reply()
                if reply is NOT_READY:
                    return
                if not self._waiting:
                    raise ProtocolError("reply without a request")
                future = self._waiting.popleft()
                if not future.done():  # its caller may have given up (timeout)
                    future.set_result(reply)
        except ProtocolError as exc:
            self.abort(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        self._fail(exc or ConnectionResetError("connection closed by server"))

    def submit(self, payload: bytes, replies: int) -> list[asyncio.Future[Any]]:
        if self.error is not None:
            raise self.error
        loop = asyncio.get_running_loop()
        futures = [loop.create_future() for _ in range(replies)]
        self._waiting.extend(futures)
        self._out.append(payload)
        if not self._flush_scheduled:
            self._flush_scheduled = True
            loop.call_soon(self._flush)
        return futures

    def _flush(self) -> None:
        self._flush_scheduled = False
        if self._out and self._transport is not None and self.error is None:
            self._transport.write(b"".join(self._out))
        self._out.clear()

    def abort(self, exc: Exception) -> None:
        """Fail every queued request and drop the socket.

        A reply that arrived late would otherwise be matched to the wrong
        request, so a timeout on one request ends the whole connection.
        """
        self._fail(exc)
        if self._transport is not None:
            self._transport.abort()

    def _fail(self, exc: Exception) -> None:
        if self.error is None:
            self.error = exc
        while self._waiting:
            future = self._waiting.popleft()
            if not future.done():
                future.set_exception(exc)


class KVClient:
    """A lazily opened, multiplexed connection to one node.

    Any transport failure or timeout drops the connection and fails every
    request queued on it; the next call reconnects. A request that could not
    even connect raises :class:`ConnectFailedError`: it was never sent, so a
    caller may safely retry it.
    """

    def __init__(self, host: str, port: int, *, timeout_s: float = 2.0) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self._conn: _Connection | None = None
        self._connect_lock = asyncio.Lock()

    @classmethod
    def from_address(cls, address: str, *, timeout_s: float = 2.0) -> Self:
        host, _, port = address.rpartition(":")
        return cls(host, int(port), timeout_s=timeout_s)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.closed

    async def execute(self, *args: Any) -> Any:
        """Run one command; an error reply is raised as the matching exception."""
        (reply,) = await self.pipeline([args])
        if isinstance(reply, KVStoreError):
            raise reply
        return reply

    async def pipeline(self, commands: Sequence[Sequence[Any]]) -> list[Any]:
        """Send the commands back to back and return their replies, in order.

        Error replies are returned in place (as exception instances), not raised.
        """
        conn = await self._connection()
        payload = b"".join(encode_command(cmd) for cmd in commands)
        try:
            futures = conn.submit(payload, len(commands))
            return list(await asyncio.wait_for(asyncio.gather(*futures), self.timeout_s))
        except TimeoutError as exc:
            conn.abort(exc)
            raise self._unavailable(exc) from exc
        except (OSError, EOFError, ProtocolError) as exc:
            raise self._unavailable(exc) from exc

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.abort(ConnectionAbortedError("client closed"))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def _connection(self) -> _Connection:
        conn = self._conn
        if conn is not None and not conn.closed:
            return conn
        async with self._connect_lock:
            conn = self._conn
            if conn is None or conn.closed:
                loop = asyncio.get_running_loop()
                try:
                    _, fresh = await asyncio.wait_for(
                        loop.create_connection(_Connection, self.host, self.port),
                        self.timeout_s,
                    )
                except (OSError, TimeoutError) as exc:
                    raise ConnectFailedError(self._reason(exc)) from exc
                self._conn = conn = fresh
            return conn

    def _unavailable(self, exc: BaseException) -> NodeUnavailableError:
        return NodeUnavailableError(self._reason(exc))

    def _reason(self, exc: BaseException) -> str:
        reason = type(exc).__name__ if isinstance(exc, TimeoutError) else str(exc)
        return f"{self.address} unavailable: {reason}"


class KVClientPool:
    """A few multiplexed connections to one node, used round-robin.

    One multiplexed connection already carries many requests at once; a
    second one keeps a huge reply on one socket from delaying every other
    request behind it. A caller that needs ordering (a client's pipeline)
    sends it in one :meth:`pipeline` call, which uses a single connection.
    """

    def __init__(self, address: str, *, size: int = 2, timeout_s: float = 2.0) -> None:
        if size < 1:
            raise ValueError("pool size must be at least 1")
        self.address = address
        self._clients = [KVClient.from_address(address, timeout_s=timeout_s) for _ in range(size)]
        self._next = itertools.cycle(self._clients)

    async def pipeline(self, commands: Sequence[Sequence[Any]]) -> list[Any]:
        return await next(self._next).pipeline(commands)

    async def execute(self, *args: Any) -> Any:
        return await next(self._next).execute(*args)

    async def close(self) -> None:
        await asyncio.gather(*(client.close() for client in self._clients))
