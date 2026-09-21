"""Binary point-in-time snapshot of the keyspace (the analogue of Redis's RDB file).

Layout (little-endian)::

    header   "KVSNAP" | u16 version | i64 created_ms | u64 key_count
    record*  u8 type | i64 expires_ms (-1 = none) | str key | payload
               string: str          list/set: u32 n, n * str
               hash:   u32 n, n * (str field, str value)
               zset:   u32 n, n * (str member, f64 score)
    footer   u8 0xFF | u32 crc32(everything before the crc)
    str      u32 length | UTF-8 bytes

The file is written to ``<name>.tmp``, fsynced, then atomically renamed, so
a crash mid-write never leaves a half-written snapshot under the real name.
The CRC is verified *before* any record is loaded.
"""

from __future__ import annotations

import io
import os
import struct
import zlib
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import IO, Any

from kvstore.core.codec import SnapshotRecord, to_bytes, to_str
from kvstore.core.exceptions import SnapshotCorruptedError

MAGIC = b"KVSNAP"
VERSION = 1
_EOF = 0xFF
_TYPE_CODES = {"string": 0, "list": 1, "hash": 2, "set": 3, "zset": 4}
_TYPE_NAMES = {code: name for name, code in _TYPE_CODES.items()}

_HEADER = struct.Struct("<6sHqQ")
_RECORD = struct.Struct("<Bq")
_U32 = struct.Struct("<I")
_F64 = struct.Struct("<d")
_FLUSH_AT = 1 << 20


class _ChecksummedWriter:
    def __init__(self, file: IO[bytes]) -> None:
        self._file = file
        self._buf = bytearray()
        self.crc = 0
        self.size = 0

    def write(self, data: bytes) -> None:
        self._buf += data
        if len(self._buf) >= _FLUSH_AT:
            self.flush()

    def write_str(self, value: str) -> None:
        data = to_bytes(value)
        self.write(_U32.pack(len(data)))
        self.write(data)

    def flush(self) -> None:
        self.crc = zlib.crc32(self._buf, self.crc)
        self._file.write(self._buf)
        self.size += len(self._buf)
        self._buf.clear()


def write_snapshot(path: Path, records: Sequence[SnapshotRecord], *, created_at: float) -> int:
    """Write the snapshot atomically; returns its size in bytes."""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as file:
        size = _write(file, records, created_at)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)
    return size


def encode_snapshot(records: Sequence[SnapshotRecord], *, created_at: float) -> bytes:
    """The same format in memory: what a primary sends a replica on a full resync."""
    buffer = io.BytesIO()
    _write(buffer, records, created_at)
    return buffer.getvalue()


def _write(file: IO[bytes], records: Sequence[SnapshotRecord], created_at: float) -> int:
    out = _ChecksummedWriter(file)
    out.write(_HEADER.pack(MAGIC, VERSION, round(created_at * 1000), len(records)))
    for key, kind, payload, expires_at in records:
        expires_ms = -1 if expires_at is None else round(expires_at * 1000)
        out.write(_RECORD.pack(_TYPE_CODES[kind], expires_ms))
        out.write_str(key)
        _write_payload(out, kind, payload)
    out.write(bytes([_EOF]))
    out.flush()
    file.write(_U32.pack(out.crc))
    return out.size + _U32.size


def _write_payload(out: _ChecksummedWriter, kind: str, payload: Any) -> None:
    if kind == "string":
        out.write_str(payload)
        return
    out.write(_U32.pack(len(payload)))
    if kind in ("list", "set"):
        for item in payload:
            out.write_str(item)
    elif kind == "hash":
        for field, value in payload:
            out.write_str(field)
            out.write_str(value)
    else:  # zset
        for member, score in payload:
            out.write_str(member)
            out.write(_F64.pack(score))


def read_snapshot(path: Path) -> Iterator[SnapshotRecord]:
    """Yield every record; raises :class:`SnapshotCorruptedError` before yielding anything bad."""
    return decode_snapshot(path.read_bytes(), source=str(path))


def decode_snapshot(raw: bytes, *, source: str = "snapshot") -> Iterator[SnapshotRecord]:
    """Decode an in-memory snapshot, checking its CRC before yielding any record."""
    data = memoryview(raw)
    path = source
    if len(data) < _HEADER.size + 1 + _U32.size:
        raise SnapshotCorruptedError(f"{path}: file too short")
    body, (expected_crc,) = data[: -_U32.size], _U32.unpack(data[-_U32.size :])
    if zlib.crc32(body) != expected_crc:
        raise SnapshotCorruptedError(f"{path}: checksum mismatch")
    magic, version, _created_ms, count = _HEADER.unpack_from(body, 0)
    if magic != MAGIC or version != VERSION:
        raise SnapshotCorruptedError(f"{path}: not a v{VERSION} snapshot")
    reader = _Reader(body, _HEADER.size)
    try:
        for _ in range(count):
            code, expires_ms = reader.unpack(_RECORD)
            kind = _TYPE_NAMES[code]
            key = reader.read_str()
            yield (
                key,
                kind,
                _read_payload(reader, kind),
                (None if expires_ms < 0 else expires_ms / 1000),
            )
        if reader.read_byte() != _EOF:
            raise SnapshotCorruptedError(f"{path}: missing end marker")
    except (KeyError, struct.error, IndexError) as exc:
        raise SnapshotCorruptedError(f"{path}: malformed record: {exc!r}") from exc


def _read_payload(reader: _Reader, kind: str) -> Any:
    if kind == "string":
        return reader.read_str()
    (count,) = reader.unpack(_U32)
    if kind in ("list", "set"):
        return [reader.read_str() for _ in range(count)]
    if kind == "hash":
        return [(reader.read_str(), reader.read_str()) for _ in range(count)]
    return [(reader.read_str(), reader.unpack(_F64)[0]) for _ in range(count)]


class _Reader:
    def __init__(self, data: memoryview, offset: int) -> None:
        self._data = data
        self._pos = offset

    def unpack(self, fmt: struct.Struct) -> tuple[Any, ...]:
        values = fmt.unpack_from(self._data, self._pos)
        self._pos += fmt.size
        return values

    def read_str(self) -> str:
        (size,) = self.unpack(_U32)
        end = self._pos + size
        if end > len(self._data):
            raise IndexError("string runs past the end of the file")
        value = to_str(bytes(self._data[self._pos : end]))
        self._pos = end
        return value

    def read_byte(self) -> int:
        value = self._data[self._pos]
        self._pos += 1
        return value


def fsync_dir(directory: Path) -> None:
    """Make a rename durable. Not supported on Windows, where it is a no-op."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ------------------------------------------------------- single values
_DUMP_VERSION = 1


def dump_value(kind: str, payload: Any) -> bytes:
    """One value in the snapshot encoding plus a CRC: the payload of DUMP / RESTORE."""
    buffer = io.BytesIO()
    out = _ChecksummedWriter(buffer)
    out.write(bytes([_DUMP_VERSION, _TYPE_CODES[kind]]))
    _write_payload(out, kind, payload)
    out.flush()
    buffer.write(_U32.pack(out.crc))
    return buffer.getvalue()


def load_value(data: bytes) -> tuple[str, Any]:
    """Decode :func:`dump_value`'s output; raises ``ValueError`` if it is damaged."""
    if len(data) < 2 + _U32.size:
        raise ValueError("DUMP payload too short")
    body, (expected,) = memoryview(data)[: -_U32.size], _U32.unpack(data[-_U32.size :])
    if zlib.crc32(body) != expected or body[0] != _DUMP_VERSION:
        raise ValueError("DUMP payload version or checksum not valid")
    try:
        kind = _TYPE_NAMES[body[1]]
        reader = _Reader(body, 2)
        payload = _read_payload(reader, kind)
    except (KeyError, struct.error, IndexError) as exc:
        raise ValueError(f"malformed DUMP payload: {exc!r}") from exc
    return kind, payload
