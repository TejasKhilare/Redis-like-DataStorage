# ADR-0004: A stateless router with consistent hashing

**Status:** accepted (Phase 1); amended in Phase 4 by ADR-0008 to ADR-0011 (the ring now holds shard-group ids; replication, failover, rebalancing, pooled multiplexed connections)

## Context
Keys must be spread across shards so that adding or removing a shard doesn't
reshuffle everything. Clients shouldn't need to know the topology.

## Decision
- A **stateless router** hashes every key onto a consistent-hash ring:
  - 64-bit MD5 prefix; `usedforsecurity=False`;
  - 100 virtual nodes per shard;
  - lookups are a binary search.
- The router reads **key positions from the shared command table**, so it can
  route any command, and rejects bad requests (unknown command, wrong arity)
  without a network hop.
- **Multi-key commands must stay on one shard.** Otherwise the router answers
  `CROSS_SHARD`, the equivalent of Redis Cluster's `CROSSSLOT`, rather than
  pretending a cross-shard `DEL` is atomic.
- **Failures are isolated per shard:**
  - an unreachable or slow shard fails only its own keys, with
    `NODE_UNAVAILABLE` (HTTP 503);
  - a timeout drops the connection, so a late reply is never read as the
    answer to the next request;
  - `/ready` returns `degraded` (200) while some shards are down, so the
    router stays in the load-balancer pool, and `unavailable` (503) only when
    all of them are down.

## Consequences
- ✅ Any number of routers can run. Adding a shard moves about 1/N of the
  keys; tests check that adding a node only moves keys to that node.
- ✅ With 100 virtual nodes per shard, each shard's key count stays within
  ±15% of the ideal in tests.
- ⚠️ An extra network hop per request. The alternative is client-side routing
  with a topology map, as Redis Cluster's `MOVED` redirects provide; we may
  revisit it after Phase 3 benchmarks.
- ⚠️ Topology is static config (`KV_SHARDS`). Adding a node doesn't yet move
  existing data (Phase 4: rebalancing).
- ⚠️ No replication yet, so a down shard's keys are unavailable (Phase 4: replicas and failover).
- ⚠️ One connection per shard, with requests serialized on it. Phase 4 adds
  connection pooling.
- **Alternative considered:** fixed hash slots, like Redis Cluster's 16384.
  They make rebalancing explicit (move slots rather than ranges) and are
  likely the better fit once rebalancing lands.
