"""Minimal wire clients for the load generator: RESP and HTTP/1.1 keep-alive.

They are deliberately bare -- an ``asyncio.Protocol`` that writes pre-built
requests and only *frames* replies (counting them and noting errors, never
decoding values) -- so that the client spends as little CPU per request as
possible and the server stays the bottleneck. redis-py or httpx would cost
several times more per request than the server under test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal, cast

Protocol = Literal["resp", "http"]

# (buffer, start) -> (end of this reply, is_error), or None if incomplete.
Scanner = Callable[[bytearray, int], tuple[int, bool] | None]


class FramingError(Exception):
    """The server sent something that isn't a valid reply."""


# ------------------------------------------------------------------ RESP
def scan_resp(buf: bytearray, pos: int) -> tuple[int, bool] | None:
    eol = buf.find(b"\r\n", pos)
    if eol < 0:
        return None
    kind = buf[pos]
    after = eol + 2
    if kind in b"+:":
        return after, False
    if kind == 0x2D:  # '-'
        return after, True
    if kind == 0x24:  # '$'
        size = int(buf[pos + 1 : eol])
        if size < 0:
            return after, False
        end = after + size + 2
        return (end, False) if end <= len(buf) else None
    if kind == 0x2A:  # '*'
        count = int(buf[pos + 1 : eol])
        error = False
        for _ in range(max(count, 0)):
            item = scan_resp(buf, after)
            if item is None:
                return None
            after, item_error = item
            error = error or item_error
        return after, error
    raise FramingError(f"unexpected RESP type byte {chr(kind)!r}")


def resp_command(*parts: bytes) -> bytes:
    out = [b"*%d\r\n" % len(parts)]
    out.extend(b"$%d\r\n%s\r\n" % (len(part), part) for part in parts)
    return b"".join(out)


# ------------------------------------------------------------------ HTTP
def scan_http(buf: bytearray, pos: int) -> tuple[int, bool] | None:
    head_end = buf.find(b"\r\n\r\n", pos)
    if head_end < 0:
        return None
    if buf[pos : pos + 5] != b"HTTP/":
        raise FramingError("expected an HTTP status line")
    status = int(buf[pos + 9 : pos + 12])
    head = bytes(buf[pos:head_end]).lower()
    marker = head.find(b"\r\ncontent-length:")
    if marker < 0:
        if b"\r\ntransfer-encoding:" in head:
            raise FramingError("chunked responses are not supported")
        length = 0
    else:
        start = marker + len(b"\r\ncontent-length:")
        line_end = head.find(b"\r\n", start)
        length = int(head[start : line_end if line_end >= 0 else len(head)])
    end = head_end + 4 + length
    if end > len(buf):
        return None
    # 404 is a cache miss (a GET of an absent key), not a failure.
    return end, status >= 400 and status != 404


def http_get(key: bytes) -> bytes:
    return b"GET /v1/keys/%s HTTP/1.1\r\nHost: kvstore\r\n\r\n" % key


def http_put(key: bytes, value: bytes) -> bytes:
    body = b'{"value":"%s"}' % value  # values are ASCII filler, so no JSON escaping needed
    return (
        b"PUT /v1/keys/%s HTTP/1.1\r\nHost: kvstore\r\n"
        b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (key, len(body), body)
    )


class RequestEncoder:
    """Builds GET/SET requests for one protocol."""

    def __init__(self, protocol: Protocol, value: bytes) -> None:
        self.protocol = protocol
        self._value = value

    def get(self, key: bytes) -> bytes:
        return resp_command(b"GET", key) if self.protocol == "resp" else http_get(key)

    def set(self, key: bytes) -> bytes:
        if self.protocol == "resp":
            return resp_command(b"SET", key, self._value)
        return http_put(key, self._value)


# ------------------------------------------------------------ connection
class Connection(asyncio.Protocol):
    """One TCP connection that sends a batch and waits for all its replies."""

    def __init__(self, scanner: Scanner) -> None:
        self._scanner = scanner
        self._transport: asyncio.Transport | None = None
        self._buf = bytearray()
        self._waiter: asyncio.Future[int] | None = None
        self._expected = 0
        self._errors = 0
        self._lost: Exception | None = None

    @classmethod
    async def open(cls, host: str, port: int, protocol: Protocol) -> Connection:
        loop = asyncio.get_running_loop()
        scanner = scan_resp if protocol == "resp" else scan_http
        _, conn = await loop.create_connection(lambda: cls(scanner), host, port)
        return conn

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # uvloop's transports implement the interface without subclassing it.
        self._transport = cast(asyncio.Transport, transport)

    def data_received(self, data: bytes) -> None:
        self._buf += data
        pos = 0
        try:
            while self._expected:
                frame = self._scanner(self._buf, pos)
                if frame is None:
                    break
                pos, is_error = frame
                self._errors += is_error
                self._expected -= 1
        except (FramingError, ValueError) as exc:
            self._fail(exc)
            return
        if pos:
            del self._buf[:pos]
        if self._expected == 0 and self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(self._errors)

    def connection_lost(self, exc: Exception | None) -> None:
        self._lost = exc or ConnectionResetError("connection closed by server")
        self._fail(self._lost)

    def _fail(self, exc: Exception) -> None:
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_exception(exc)

    async def send(self, payload: bytes, replies: int) -> int:
        """Write ``payload`` and wait for ``replies`` replies; returns how many were errors."""
        if self._lost is not None or self._transport is None:
            raise ConnectionError("connection is closed") from self._lost
        self._waiter = asyncio.get_running_loop().create_future()
        self._expected = replies
        self._errors = 0
        self._transport.write(payload)
        return await self._waiter

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
