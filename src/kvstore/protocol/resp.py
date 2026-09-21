"""RESP2, the Redis serialization protocol.

Requests are arrays of bulk strings (``*2\\r\\n$3\\r\\nGET\\r\\n$1\\r\\nk\\r\\n``)
or "inline" commands (``PING\\r\\n``, what telnet and ``redis-benchmark``'s
PING_INLINE send). Replies are one of:

    +simple string   -error   :integer   $bulk string ($-1 = nil)   *array (*-1 = nil)

Strings cross the wire as bytes. They are decoded as UTF-8 with
``surrogateescape``, so any byte sequence -- valid UTF-8 or not -- survives a
round trip unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from kvstore.core.codec import to_bytes, to_str
from kvstore.core.exceptions import KVStoreError, ProtocolError

CRLF: Final = b"\r\n"

DEFAULT_MAX_BULK = 64 * 1024 * 1024
MAX_INLINE = 64 * 1024
MAX_ARRAY = 1024 * 1024


class SimpleString(str):
    """A status reply (``+OK``), as opposed to a bulk string reply."""

    __slots__ = ()


OK: Final = SimpleString("OK")
PONG: Final = SimpleString("PONG")


def format_float(value: float) -> str:
    """Shortest string that round-trips, the way Redis 7 prints scores."""
    if value == float("inf"):
        return "inf"
    if value == float("-inf"):
        return "-inf"
    if value.is_integer() and abs(value) < 1e17:
        return str(int(value))
    return repr(value)


# ---------------------------------------------------------------- encoding
def encode_reply(value: Any) -> bytes:
    out: list[bytes] = []
    _encode(value, out)
    return b"".join(out)


def encode_error(error: KVStoreError) -> bytes:
    line = error.to_resp().replace("\r", " ").replace("\n", " ")
    return b"-" + to_bytes(line) + CRLF


def encode_command(args: Sequence[Any]) -> bytes:
    """A request: an array of bulk strings (numbers are sent as their decimal text)."""
    out = [b"*%d\r\n" % len(args)]
    for arg in args:
        if isinstance(arg, bytes):
            data = arg
        elif isinstance(arg, float):
            data = format_float(arg).encode()
        else:
            data = to_bytes(str(arg))
        out.append(b"$%d\r\n%s\r\n" % (len(data), data))
    return b"".join(out)


def _encode(value: Any, out: list[bytes]) -> None:
    # Ordered by frequency; SimpleString must precede str, bool is an int.
    if value is None:
        out.append(b"$-1\r\n")
    elif isinstance(value, SimpleString):
        out.append(b"+%s\r\n" % to_bytes(value))
    elif isinstance(value, str):
        data = to_bytes(value)
        out.append(b"$%d\r\n%s\r\n" % (len(data), data))
    elif isinstance(value, int):
        out.append(b":%d\r\n" % value)
    elif isinstance(value, list | tuple):
        out.append(b"*%d\r\n" % len(value))
        for item in value:
            _encode(item, out)
    elif isinstance(value, float):
        _encode(format_float(value), out)
    elif isinstance(value, bytes):
        out.append(b"$%d\r\n%s\r\n" % (len(value), value))
    elif isinstance(value, KVStoreError):
        out.append(encode_error(value))
    else:
        raise TypeError(f"cannot encode {type(value).__name__} as RESP")


# ------------------------------------------------------ request parsing
class RequestParser:
    """Incremental parser for client requests.

    ``feed()`` raw bytes as they arrive, then call ``next_command()`` until it
    returns ``None`` (need more data). Progress through a partially received
    multi-bulk request is kept, so a large value arriving in many chunks is
    not re-parsed from the start each time.
    """

    def __init__(self, max_bulk: int = DEFAULT_MAX_BULK) -> None:
        self._buf = bytearray()
        self._pos = 0
        self._max_bulk = max_bulk
        self._pending: list[str] | None = None
        self._remaining = 0

    def feed(self, data: bytes) -> None:
        self._buf += data

    def next_command(self) -> list[str] | None:
        """The next complete command (``[]`` for an empty line), or ``None``."""
        buf = self._buf
        if self._pending is None:
            if self._pos >= len(buf):
                self._compact()
                return None
            if buf[self._pos] != 0x2A:  # '*'
                return self._parse_inline()
            eol = buf.find(CRLF, self._pos)
            if eol < 0:
                self._check_line_length()
                return None
            count = _parse_int(buf, self._pos + 1, eol, "invalid multibulk length")
            self._pos = eol + 2
            if count <= 0:
                return []
            if count > MAX_ARRAY:
                raise ProtocolError("invalid multibulk length")
            self._pending, self._remaining = [], count

        while self._remaining:
            pos = self._pos
            if pos >= len(buf):
                return None
            if buf[pos] != 0x24:  # '$'
                raise ProtocolError(f"expected '$', got '{chr(buf[pos])}'")
            eol = buf.find(CRLF, pos)
            if eol < 0:
                self._check_line_length()
                return None
            size = _parse_int(buf, pos + 1, eol, "invalid bulk length")
            if size < 0 or size > self._max_bulk:
                raise ProtocolError("invalid bulk length")
            start = eol + 2
            end = start + size
            if end + 2 > len(buf):
                return None
            if buf[end : end + 2] != CRLF:
                raise ProtocolError("invalid bulk string terminator")
            self._pending.append(to_str(buf[start:end]))
            self._pos = end + 2
            self._remaining -= 1

        command, self._pending = self._pending, None
        self._compact()
        return command

    def _parse_inline(self) -> list[str] | None:
        eol = self._buf.find(b"\n", self._pos)
        if eol < 0:
            self._check_line_length()
            return None
        line = self._buf[self._pos : eol].rstrip(b"\r")
        self._pos = eol + 1
        self._compact()
        return [to_str(part) for part in line.split()]

    def _check_line_length(self) -> None:
        if len(self._buf) - self._pos > MAX_INLINE:
            raise ProtocolError("too big request line")

    def _compact(self) -> None:
        if self._pos == len(self._buf):
            self._buf.clear()
            self._pos = 0
        elif self._pos > 64 * 1024:
            del self._buf[: self._pos]
            self._pos = 0


def _parse_int(buf: bytearray, start: int, end: int, error: str) -> int:
    try:
        return int(buf[start:end])
    except ValueError:
        raise ProtocolError(error) from None


# -------------------------------------------------------- reply parsing
class _NotReady:
    __slots__ = ()


NOT_READY: Final = _NotReady()


class ReplyParser:
    """Incremental parser for server replies (used by clients and the router).

    Error replies are *returned* as :class:`KVStoreError` instances, not
    raised: they are valid replies. Only a malformed stream raises
    :class:`ProtocolError`.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._pos = 0

    def feed(self, data: bytes) -> None:
        self._buf += data

    def next_reply(self) -> Any:
        """The next complete reply, or :data:`NOT_READY`."""
        result = self._parse(self._pos)
        if result is NOT_READY:
            return NOT_READY
        value, self._pos = result
        if self._pos == len(self._buf):
            self._buf.clear()
            self._pos = 0
        elif self._pos > 64 * 1024:
            del self._buf[: self._pos]
            self._pos = 0
        return value

    def _parse(self, pos: int) -> Any:
        buf = self._buf
        eol = buf.find(CRLF, pos)
        if eol < 0:
            return NOT_READY
        kind, line = buf[pos], buf[pos + 1 : eol]
        after = eol + 2
        if kind == 0x2B:  # '+'
            return SimpleString(to_str(line)), after
        if kind == 0x2D:  # '-'
            return KVStoreError.from_resp(to_str(line)), after
        if kind == 0x3A:  # ':'
            return _parse_int(buf, pos + 1, eol, "invalid integer reply"), after
        if kind == 0x24:  # '$'
            size = _parse_int(buf, pos + 1, eol, "invalid bulk length")
            if size < 0:
                return None, after
            if after + size + 2 > len(buf):
                return NOT_READY
            return to_str(buf[after : after + size]), after + size + 2
        if kind == 0x2A:  # '*'
            count = _parse_int(buf, pos + 1, eol, "invalid multibulk length")
            if count < 0:
                return None, after
            items: list[Any] = []
            for _ in range(count):
                result = self._parse(after)
                if result is NOT_READY:
                    return NOT_READY
                item, after = result
                items.append(item)
            return items, after
        raise ProtocolError(f"unexpected reply type byte {chr(kind)!r}")
