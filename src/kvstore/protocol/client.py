"""Async client for the TCP data plane (used by the router, the CLI and tests)."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, Self

from kvstore.core.exceptions import NodeUnavailableError, ProtocolError
from kvstore.protocol.json_lines import Response, decode_response, encode_request

_MAX_RESPONSE_BYTES = 64 * 1024 * 1024


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
        self._lock = asyncio.Lock()

    @classmethod
    def from_address(cls, address: str, *, timeout_s: float = 2.0) -> Self:
        host, _, port = address.rpartition(":")
        return cls(host, int(port), timeout_s=timeout_s)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    async def execute(self, command: str, *args: Any) -> Any:
        async with self._lock:
            try:
                response = await asyncio.wait_for(
                    self._roundtrip(command, list(args)), self.timeout_s
                )
            except (OSError, TimeoutError, EOFError, ValueError, ProtocolError) as exc:
                await self._disconnect()
                reason = type(exc).__name__ if isinstance(exc, TimeoutError) else str(exc)
                raise NodeUnavailableError(f"{self.address} unavailable: {reason}") from exc
        return response.unwrap()

    async def close(self) -> None:
        async with self._lock:
            await self._disconnect()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def _roundtrip(self, command: str, args: list[Any]) -> Response:
        if self._writer is None or self._reader is None:
            self._reader, self._writer = await asyncio.open_connection(
                self.host, self.port, limit=_MAX_RESPONSE_BYTES
            )
        self._writer.write(encode_request(command, args))
        await self._writer.drain()
        line = await self._reader.readline()
        if not line:
            raise ConnectionResetError("connection closed by server")
        return decode_response(line)

    async def _disconnect(self) -> None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
