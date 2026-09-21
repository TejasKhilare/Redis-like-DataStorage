"""The replication stream: its backlog and its framing.

The stream is the sequence of effects a primary applies (the same records
its AOF gets), each encoded as a RESP array. Every byte has an *offset*:
the number of stream bytes produced before it. A replica that has applied
the stream up to offset N holds exactly the primary's state at N.
"""

from __future__ import annotations

import secrets

from kvstore.core.codec import to_str
from kvstore.core.exceptions import ProtocolError


def new_replid() -> str:
    """A replication id: names one history of the stream (40 hex chars, like Redis)."""
    return secrets.token_hex(20)


class Backlog:
    """The last ``capacity`` bytes of the stream, addressable by offset.

    A replica that reconnects asking to continue from offset N can be served
    from here (a *partial* resync) as long as N is still inside it;
    otherwise it needs a full resync.
    """

    def __init__(self, capacity: int, *, offset: int = 0) -> None:
        if capacity <= 0:
            raise ValueError("backlog capacity must be positive")
        self.capacity = capacity
        self._buf = bytearray()
        self.start = offset  # offset of the first byte held
        self.end = offset  # offset after the last byte held (= the stream offset)

    def __len__(self) -> int:
        return len(self._buf)

    def append(self, data: bytes) -> None:
        self._buf += data
        self.end += len(data)
        excess = len(self._buf) - self.capacity
        if excess > 0:
            del self._buf[:excess]
            self.start += excess

    def contains(self, offset: int) -> bool:
        return self.start <= offset <= self.end

    def since(self, offset: int) -> bytes | None:
        """Every byte from ``offset`` on, or ``None`` if they are no longer held."""
        if not self.contains(offset):
            return None
        return bytes(self._buf[offset - self.start :])

    def reset(self, offset: int) -> None:
        """Start over at ``offset`` (after a full resync: nothing before it is known)."""
        self._buf.clear()
        self.start = self.end = offset


def next_command(buf: bytearray, pos: int) -> tuple[list[str], int] | None:
    """The command at ``pos`` and the position after it, or ``None`` if incomplete.

    The stream only carries arrays of bulk strings, which is all this accepts.
    """
    if pos >= len(buf):
        return None
    if buf[pos] != 0x2A:  # '*'
        raise ProtocolError(f"expected an array in the replication stream, got {buf[pos]!r}")
    eol = buf.find(b"\r\n", pos)
    if eol < 0:
        return None
    count = int(buf[pos + 1 : eol])
    cursor = eol + 2
    args: list[str] = []
    for _ in range(count):
        if cursor >= len(buf):
            return None
        if buf[cursor] != 0x24:  # '$'
            raise ProtocolError("expected a bulk string in the replication stream")
        eol = buf.find(b"\r\n", cursor)
        if eol < 0:
            return None
        start = eol + 2
        end = start + int(buf[cursor + 1 : eol])
        if end + 2 > len(buf):
            return None
        args.append(to_str(buf[start:end]))
        cursor = end + 2
    return args, cursor
