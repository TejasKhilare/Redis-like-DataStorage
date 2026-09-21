"""Byte <-> str conversion used at every boundary (wire, disk).

UTF-8 with ``surrogateescape``: valid UTF-8 becomes normal text, any other
byte becomes a lone surrogate, and encoding restores the original bytes.
So values stay binary-safe while the engine works with plain ``str``.
"""

from __future__ import annotations

from typing import Any


def to_bytes(value: str) -> bytes:
    return value.encode("utf-8", "surrogateescape")


def to_str(data: bytes | bytearray | memoryview) -> str:
    return bytes(data).decode("utf-8", "surrogateescape")


SnapshotRecord = tuple[str, str, Any, float | None]
"""(key, type name, plain-Python copy of the value, expires_at) -- one key in a snapshot."""
