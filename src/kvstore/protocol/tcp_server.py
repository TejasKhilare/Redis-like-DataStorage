"""asyncio RESP server for the data plane (shared by shards and the router)."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractContextManager, nullcontext, suppress
from dataclasses import dataclass
from typing import Any

from kvstore.core.exceptions import KVStoreError, ProtocolError
from kvstore.observability.metrics import SIZE_BUCKETS, Histogram
from kvstore.protocol.resp import OK, RequestParser, encode_error, encode_reply

logger = logging.getLogger(__name__)

CommandHandler = Callable[..., Any]
"""``handler(command, *args)`` -> result (or an awaitable of it); raises :class:`KVStoreError`."""

BatchHandler = Callable[[Sequence[list[str]]], Awaitable[list[Any]]]
"""``handler(commands)`` -> one result per command, errors returned in place as exceptions."""

BatchContext = Callable[[], AbstractContextManager[None]]


@dataclass(frozen=True, slots=True)
class Takeover:
    """A command result that takes the connection over.

    ``PSYNC`` returns one: from then on the socket carries a replication
    stream, not request/reply traffic. Replies to the commands before it are
    sent first; commands after it in the same read are dropped.
    """

    run: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


_READ_SIZE = 64 * 1024
_SHUTDOWN_TIMEOUT_S = 5.0


class GroupCommit:
    """One AOF commit (and fsync) for every connection served in an event-loop iteration.

    A connection joins before running its batch and waits for the commit
    before replying. The first to join schedules the commit with
    ``call_soon``, so it runs after every other connection woken in the
    same iteration has run its batch -- the equivalent of Redis flushing the
    AOF once in ``beforeSleep`` for all clients. Under ``appendfsync
    always``, N clients writing at once share one fsync instead of N.
    """

    def __init__(self, begin: Callable[[], None], commit: Callable[[], None]) -> None:
        self._begin = begin
        self._commit = commit
        self._waiter: asyncio.Future[None] | None = None
        self._joined = 0
        self.commits = 0
        self.batch_sizes = Histogram(SIZE_BUCKETS)  # batches (connections) per commit
        self.commit_seconds = Histogram()

    def join(self) -> asyncio.Future[None]:
        """Join the pending commit; await the result before sending any reply."""
        if self._waiter is None:
            loop = asyncio.get_running_loop()
            self._begin()
            self._waiter = loop.create_future()
            loop.call_soon(self._flush)
        self._joined += 1
        return self._waiter

    def _flush(self) -> None:
        waiter, self._waiter = self._waiter, None
        assert waiter is not None
        self.commits += 1
        self.batch_sizes.observe(self._joined)
        self._joined = 0
        started = time.perf_counter()
        try:
            self._commit()
        except Exception as exc:
            waiter.set_exception(exc)
            waiter.add_done_callback(lambda f: f.exception())  # retrieved even if nobody waits
        else:
            waiter.set_result(None)
        finally:
            self.commit_seconds.observe(time.perf_counter() - started)


class TCPServer:
    """Serves RESP over TCP.

    Every read may carry many pipelined commands; all of them run before any
    reply is written. There are three ways to run them:

    * ``batch_handler`` (the router): the whole batch at once, so commands
      for different shards travel in parallel and those for one shard share
      a round trip;
    * ``group_commit`` (a shard): commands run synchronously, without
      yielding, so no other client's commands interleave with the batch; then
      the connection waits for the shared AOF commit before replying;
    * ``handler`` alone, optionally inside a ``batch`` context manager.
    """

    def __init__(
        self,
        handler: CommandHandler,
        *,
        host: str,
        port: int,
        max_request_bytes: int = 64 * 1024 * 1024,
        batch: BatchContext | None = None,
        batch_handler: BatchHandler | None = None,
        group_commit: GroupCommit | None = None,
    ) -> None:
        self._handler = handler
        self._host = host
        self._port = port
        self._max_request_bytes = max_request_bytes
        self._batch: BatchContext = batch or nullcontext
        self._batch_handler = batch_handler
        self._group_commit = group_commit
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
                commands, trailer, close = self._drain(parser)
                replies = await self._run(commands) if commands else []
                takeover = next((r for r in replies if isinstance(r, Takeover)), None)
                if takeover is not None:
                    before = replies[: replies.index(takeover)]
                    writer.write(b"".join(r for r in before if isinstance(r, bytes)))
                    await writer.drain()
                    await takeover.run(reader, writer)
                    break
                out = [r for r in replies if isinstance(r, bytes)] + trailer
                if out:
                    writer.write(b"".join(out))
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

    @staticmethod
    def _drain(parser: RequestParser) -> tuple[list[list[str]], list[bytes], bool]:
        """Every complete command received so far, plus a final reply that ends the batch.

        The trailer is ``QUIT``'s OK or a protocol error; either one closes the
        connection after the commands before it have been answered.
        """
        commands: list[list[str]] = []
        while True:
            try:
                command = parser.next_command()
            except ProtocolError as exc:
                return commands, [encode_error(exc)], True  # out of sync: hang up, like Redis
            if command is None:
                return commands, [], False
            if not command:
                continue
            if command[0].upper() == "QUIT":
                return commands, [encode_reply(OK)], True
            commands.append(command)

    async def _run(self, commands: list[list[str]]) -> list[bytes | Takeover]:
        if self._batch_handler is not None:
            results = await self._batch_handler(commands)
            return [_encode_result(result) for result in results]
        if self._group_commit is not None:
            committed = self._group_commit.join()
            replies = [await self._respond(command) for command in commands]
            await committed  # raises if the shared commit failed
            return replies
        with self._batch():
            return [await self._respond(command) for command in commands]

    async def _respond(self, command: list[str]) -> bytes | Takeover:
        try:
            result = self._handler(*command)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, Takeover):
                return result
            return encode_reply(result)
        except KVStoreError as exc:
            return encode_error(exc)
        except Exception:
            logger.exception("unhandled error while executing a command")
            return encode_error(KVStoreError("internal error"))


def _encode_result(result: Any) -> bytes:
    if isinstance(result, KVStoreError):
        return encode_error(result)
    if isinstance(result, BaseException):
        logger.error("unhandled error while executing a command", exc_info=result)
        return encode_error(KVStoreError("internal error"))
    return encode_reply(result)
