"""Async RESP client (used by the router, the CLI and tests)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from typing import Any, Self

from kvstore.core.exceptions import KVStoreError, NodeUnavailableError, ProtocolError
from kvstore.protocol.resp import NOT_READY, ReplyParser, encode_command

_READ_SIZE = 64 * 1024


class KVClient:
    """One lazily opened connection; requests on it are serialized by a lock.

    Any transport failure or timeout drops the connection: a late reply
    arriving afterwards would otherwise be read as the answer to the next
    request. The next call reconnects.
    """

    def __init__(self, host: str, port: int, *, timeout_s: float = 2.0) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._parser = ReplyParser()
        self._lock = asyncio.Lock()

    @classmethod
    def from_address(cls, address: str, *, timeout_s: float = 2.0) -> Self:
        host, _, port = address.rpartition(":")
        return cls(host, int(port), timeout_s=timeout_s)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    async def execute(self, *args: Any) -> Any:
        """Run one command; an error reply is raised as the matching exception."""
        (reply,) = await self.pipeline([args])
        if isinstance(reply, KVStoreError):
            raise reply
        return reply

    async def pipeline(self, commands: Sequence[Sequence[Any]]) -> list[Any]:
        """Send all commands in one write, then read all replies.

        Error replies are returned in place (as exception instances), not raised.
        """
        async with self._lock:
            try:
                return await asyncio.wait_for(self._roundtrip(commands), self.timeout_s)
            except (OSError, TimeoutError, EOFError, ProtocolError) as exc:
                await self._disconnect()
                reason = type(exc).__name__ if isinstance(exc, TimeoutError) else str(exc)
                raise NodeUnavailableError(f"{self.address} unavailable: {reason}") from exc

    async def close(self) -> None:
        async with self._lock:
            await self._disconnect()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def _roundtrip(self, commands: Sequence[Sequence[Any]]) -> list[Any]:
        if self._writer is None or self._reader is None:
            self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
            self._parser = ReplyParser()
        self._writer.write(b"".join(encode_command(cmd) for cmd in commands))
        await self._writer.drain()
        replies = []
        for _ in commands:
            while (reply := self._parser.next_reply()) is NOT_READY:
                data = await self._reader.read(_READ_SIZE)
                if not data:
                    raise ConnectionResetError("connection closed by server")
                self._parser.feed(data)
            replies.append(reply)
        return replies

    async def _disconnect(self) -> None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
