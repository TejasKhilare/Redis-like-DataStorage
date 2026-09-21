# ADR-0005: RESP and the value model

**Status:** accepted (Phase 2). This replaces the JSON-lines protocol from Phase 1.

## Context
Phase 1 spoke a custom protocol of newline-delimited JSON and stored arbitrary
JSON values. That had two costs:
- no standard tool could connect: not `redis-cli`, `redis-benchmark`, or any client library;
- every request paid for JSON parsing.

## Decision
- **The data plane speaks RESP2.** The server and the router accept
  multi-bulk requests and inline commands, and support pipelining. Errors
  carry Redis's prefixes (`WRONGTYPE`, `OOM`, `CROSSSLOT`, `CLUSTERDOWN`,
  `MISCONF`), and clients rebuild the matching exception from the prefix.
- **Values follow Redis's model:** a string, or a list, hash, set or sorted
  set of strings. JSON documents are stored as strings, as they are in Redis.
- **Strings are binary-safe.** Bytes are decoded as UTF-8 with
  `surrogateescape`, so any byte sequence round-trips unchanged while the
  engine works with plain `str`.
- The HTTP API keeps convenient string endpoints, and adds
  `POST /v1/commands`, which runs any command in the same command table.
- AOF files written in the old v1 format still load. Non-string values in
  them are converted to their JSON text.

## Consequences
- ✅ Unmodified redis-py 8 passes the compatibility suite against both a
  shard and the router, and `redis-benchmark` can be used for Phase 3.
- ✅ Any client in any language works with it.
- ⚠️ **Breaking change** for Phase 1 users: the old JSON-lines client no
  longer works, and HTTP `PUT /v1/keys` now takes only string values.
- ⚠️ RESP3 is not supported: `HELLO 3` gets a `NOPROTO` reply, and clients
  fall back to RESP2.
