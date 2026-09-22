# ADR-0011: A multiplexed router and group commit across clients

**Status:** accepted (Phase 4). Both changes come from Phase 3 measurements.

## Context
Phase 3 found two structural bottlenecks:
- **The router** sent every command as its own round trip, on one
  lock-protected connection per shard. Through it, throughput was 23% of a
  direct shard, and pipelining gained only 1.3×.
- **Group commit** covered only one connection's pipeline. With
  `appendfsync always`, 50 clients cost 50 fsyncs, and kvstore reached 406
  SETs/s against Redis's 7,389.

## Decision
**Multiplexed connections.** One connection carries any number of
concurrent requests:
- each request is a future in a FIFO queue, resolved in reply order, which
  RESP guarantees;
- requests made in the same event-loop iteration are written with one
  `write()` (automatic pipelining, as ioredis and Lettuce do);
- a timeout fails every request on the connection and drops it, so a late
  reply can never be matched to the wrong request;
- a small pool (`KV_SHARD_POOL_SIZE`, default 2) per node keeps one large
  reply from delaying everything behind it.

**Batches are routed whole.** The router receives a client's whole pipeline
and sends each group's commands as one pipeline, with all groups in
parallel. Fan-out commands (`DBSIZE`, `FLUSHALL`) act as barriers, so their
order within the batch is preserved.

**Retries only when safe.** A request is retried only if repeating it cannot
apply a write twice:
- it was never sent (connection refused);
- the node refused it without running it (`READONLY`, `TRYAGAIN`, `ASK`);
- every command in it is a read.

A write that timed out is reported, never retried. A retry re-routes the
command from the current config, so it follows a failover or rebalance that
happened in between.

**Write concern (optional).** With `KV_WAIT_REPLICAS=n`, the router appends
`WAIT n` to every batch that writes, on the same connection. A write is
acknowledged only once `n` replicas have it. If they don't, the write reports
`-NOREPLICAS`: it was applied on the primary but isn't confirmed.

**Group commit across clients.** A shard holds its AOF commit open while it
runs each connection's batch. The first connection to join schedules the
commit with `call_soon`, so every connection woken in the same event-loop
iteration joins it. Everyone waits for the shared commit (and fsync) before
replying. This is the equivalent of Redis's `beforeSleep` flush. The HTTP
API waits for it too.

## Consequences
Measured (Phase 3 code vs this, same machine, interleaved):
- ✅ Through the router with pipelining: **6.4×** (≈2.0k → ≈13.2k ops/s).
- ✅ `appendfsync always`, pipelined: **4.0×**.
- ✅ `appendfsync always`, 50 unpipelined clients: **12.6–13.2 writes per
  fsync instead of 1.0**, for a median of ≈8× the throughput.
- ⚠️ A reply now waits one or two extra event-loop iterations for the shared
  commit. That is microseconds, and invisible next to an fsync.
- ⚠️ A group only includes connections whose data arrived in the same poll.
  Deferring the commit one more iteration would group more, at the cost of
  latency. It wasn't done: the current grouping matches Redis's semantics.
