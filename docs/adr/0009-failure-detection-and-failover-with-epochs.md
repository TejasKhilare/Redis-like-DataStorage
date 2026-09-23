# ADR-0009: Failure detection and failover with epochs

**Status:** accepted (Phase 4)

## Context
With replicas (ADR-0008), losing a primary no longer has to take its keys
offline, but something has to notice the failure, pick a replica, promote it
and send clients there. It also has to make sure that the old primary, when
it returns, cannot keep accepting writes nobody will see: that would be
split-brain.

## Decision
**A cluster manager runs in the router process**, like a single Redis
Sentinel watching every group. It works in three steps.

1. **Heartbeats.** It sends `INFO replication` to every node every
   `KV_HEARTBEAT_INTERVAL_S` (default 0.5 s). A node silent for
   `KV_SUSPECT_AFTER_S` is *suspect*, and after `KV_DEAD_AFTER_S` (2 s) it
   is *dead*. A reply slower than the suspect threshold counts as missing,
   so a hung node looks the same as a dead one. *(Amended in Phase 5: the
   timeout used to be shorter than the heartbeat interval, and a chaos test
   showed that 100 ms of latency then failed over a healthy primary;
   ADR-0015.)*
2. **Failover.** When a group's primary is dead:
   1. it picks the healthy replica with the highest replication offset, the
      one that has lost the fewest writes;
   2. it promotes it with `REPLICAOF NO ONE EPOCH <e+1>`;
   3. it hands the new config to the router at once, saves it atomically
      (`cluster.json`) in the background, and points the other replicas at
      the new primary. *(Amended in Phase 5: the save used to come first,
      with its fsync on the router's event loop; ADR-0015.)*

   A requested failover (`POST /v1/cluster/shards/{id}/failover`) first waits,
   for up to 1 s, for a replica to catch up to the primary's offset, so a
   planned failover loses nothing.
3. **Reconciliation.** Every round, each node's reported role is compared
   with the config and corrected.

**Epochs.** Every config change increments a cluster-wide epoch, which
provides the fencing:
- A node refuses `REPLICAOF ... EPOCH <n>` for an `n` below the epoch it has
  already seen, and persists its epoch in `node.json`. A stale manager or
  router cannot undo a newer decision.
- Routers only apply a config with a newer epoch.
- A replaced primary that comes back still reports `role:master`, at an older
  epoch. Reconciliation tells it to replicate the new primary. Its history
  has diverged, so it resyncs fully (ADR-0008) and **discards the writes it
  took alone**. A test covers exactly this.

**Bounding split-brain writes.** With `KV_MIN_REPLICAS_TO_WRITE=1`, a primary
cut off from its replicas stops accepting writes after
`KV_MIN_REPLICAS_MAX_LAG_S`. The side of a partition that will lose the
failover then stops taking writes that would be discarded.

**Routing during a failover.** The router retries requests that were never
sent (connection refused) or were refused unexecuted (`-READONLY`), with
backoff. Once the promotion lands, clients' requests succeed without any
change on their side.

## Consequences
- ✅ Measured (5 runs each, BENCHMARKS.md):
  - a `kill -9` of a primary under load recovers in 1.57 s (median, max 2.03 s)
    with the defaults, which is almost entirely the 2 s detection timeout minus
    the time since the last heartbeat;
  - the other groups see no errors, no acknowledged write was lost, and the
    old primary rejoined as a replica every time.
- ✅ Detection is a trade-off exposed as settings. The fast profile
  (0.1 s heartbeats, dead after 0.5 s) recovers in 0.46 s (median), but it
  would also fail over on a brief network hiccup.
- ⚠️ **The manager is a single process.** If it is down, there are no
  failovers (routing keeps working with the last config). Its decisions are
  not replicated: running it on Raft (the plan's stretch goal) would fix
  both, and the epoch design already assumes such a leader.
- ⚠️ **Detection is not a quorum.** One observer marks a node dead, where
  Redis Sentinel needs several to agree (ODOWN). A partition between the
  manager and a healthy primary triggers an unneeded failover. It is safe,
  thanks to the epochs and fencing above, but it costs the writes in flight.
- **Alternative considered:** replicas electing a new primary among
  themselves, as Redis Cluster does. That is more autonomous, but it needs
  a majority of primaries to vote, which a 3-group demo cluster can barely
  provide.
