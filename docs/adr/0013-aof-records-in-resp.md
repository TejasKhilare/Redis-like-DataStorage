# ADR-0013: AOF records in RESP, encoded once for the log and the replicas

**Status:** accepted (Phase 5). Changes the record format of ADR-0003 and
ADR-0006.

## Context
A v2 record (kvstore 0.3) was a CRC and a JSON array on one line. Phase 3's
profile put the record's encoding among the costs that make writes more
expensive than reads. Phase 4 then added replication, whose stream carries
each write as RESP: with a replica attached, every write was encoded twice,
once as JSON for the log and once as RESP for the stream.

## Decision
**A v3 record** is a header line, then the command as a RESP array:

```
#<crc32 of the payload, 8 hex digits> <payload length>\n
*3\r\n$3\r\nSET\r\n$6\r\nuser:1\r\n$5\r\ntejas\r\n
```

- The engine encodes each effect once (`encode_command`) and hands the same
  bytes to the replication stream and, framed, to the AOF.
- The length lets replay take the payload without scanning it. The CRC
  covers the payload, and a torn tail is detected and truncated as before.
- Replay detects the version record by record (v1 `{`, v2 hex, v3 `#`), so
  an AOF written by 0.3 keeps loading and simply continues in v3. A test
  upgrades a v2 file this way.

*Alternatives:* a binary encoding (msgpack) would be smaller, but it is a
new dependency and a third format, while RESP is what the replicas already
speak. Redis's own AOF is RESP too; the header line with its CRC is what
kvstore adds.

## Consequences
**Measured** (`python -m benchmarks.micro`, WSL2, one `SET` with a 16-byte
key and a 64-byte value):

| | v2 (JSON) | v3 (RESP) | v3 / v2 |
|---|--:|--:|--:|
| encode a record | 4.59 µs | 2.83 µs | 0.62× |
| encode, with a replica attached | 6.76 µs | 3.61 µs | 0.53× |
| decode a record (replay) | 5.38 µs | 7.90 µs | 1.47× |
| record size | 102 B | 120 B | 1.18× |

(An earlier run gave similar ratios: 0.68×, 0.42× and 1.50×.)

- ✅ Writes are cheaper, and half the cost with a replica attached, where
  v2 encoded every write twice.
- ⚠️ **Replay is slower.** A RESP record is parsed in Python, and JSON's
  decoder is C. Decoding is about a third of the 20–27 µs it takes to
  replay one record (BENCHMARKS.md, "Recovery time"); the rest is running
  the command. A decoder specialised for the one shape AOF records have
  (an array of bulk strings) is the obvious next step. A snapshot avoids
  replay altogether, which is what rewrites are for.
- ⚠️ Records are 18% larger (the RESP length prefixes and `\r\n`s), so the
  AOF grows faster and reaches the auto-rewrite threshold sooner.
