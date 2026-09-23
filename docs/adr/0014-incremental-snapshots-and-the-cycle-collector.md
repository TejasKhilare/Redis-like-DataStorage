# ADR-0014: Incremental snapshots, and keeping the cycle collector out of the way

**Status:** accepted (Phase 5). Replaces step 1 of ADR-0006's rewrite.

## Context
Without `fork()`, a rewrite copied the whole keyspace on the event loop,
with every command waiting (ADR-0006). Phase 3 measured the pause at 369 ms
for 1M string keys and 475 ms for 100k hashes. A replica's full resync
(ADR-0008) takes the same copy, so a replica joining stalled its primary
too.

## Decision
**Copy in slices, with a copy-on-write barrier.**
1. **Begin**, in one step: take the list of keys (references, not values),
   bump the store's snapshot epoch, and switch the AOF to a new generation.
   The snapshot is the keyspace at this instant.
2. **Slices:** the cron loop copies values for up to `KV_SNAPSHOT_SLICE_MS`
   (2 ms, in chunks of 256 keys), then lets commands run.
3. **Barrier:** before a command, a replicated write, an expiry or an
   eviction changes or removes a key, its value as of the start is copied
   first, unless it already was. Each entry carries the epoch of the last
   snapshot it was copied into, so every entry is copied exactly once, and
   keys created after the start are left out.
4. A `FLUSHALL` during a copy finishes the copy first.

This is copy-on-write done by hand, at the granularity of keys instead of
pages. `KV_INCREMENTAL_SNAPSHOTS=false` restores the one-shot copy.

**The cycle collector.** With slices in place, stalls of 33–51 ms
remained (50k hashes, measured during development on Windows). They were
CPython's full (generation 2) collections: the copy's
allocations triggered them, each one walked every object in the keyspace,
and none found garbage. Two changes:
- A snapshot holds `gc.freeze()` until it is done. Existing objects move
  to a permanent generation that collections skip. Holds are counted,
  because a rewrite and a resync can overlap; the last release unfreezes,
  and any garbage made in between is collected then.
- Snapshot records are tuples of immutable values, which CPython stops
  tracking, so the copy doesn't feed the collector.

Collector pauses are exported per generation on `/metrics` (ADR-0012).

## Consequences
**Measured** (`python -m benchmarks.pause`, WSL2, median of 3; the stall is
the longest gap a ticker coroutine saw while a whole rewrite ran):

| keys | longest stall, one-shot copy | longest stall, incremental: each run |
|---|--:|--:|
| 10k strings | 15.6 ms | 8.8, 5.7, 8.3 ms |
| 100k strings | 215.8 ms | 8.2, 211.9, 289.9 ms |
| 1M strings | 313.0 ms | 44.5, 38.2, 391.6 ms |
| 10k hashes | 35.4 ms | 11.1, 9.2, 11.7 ms |
| 100k hashes | 291.8 ms | 62.8, 65.4, 64.1 ms |

- ✅ Most incremental rewrites stall commands for 6–65 ms. At 1M keys that
  is 38–45 ms instead of a third of a second.
- ⚠️ **3 runs of 15 still stalled for 212–392 ms**, during heavy writeback
  on the VM's disk, with no machine-wide stall. The event loop still makes
  a few filesystem calls in a rewrite (the manifest's fsync at its start
  and end, deleting the old files), and one of them is the likely cause.
  It isn't confirmed: a traced rerun didn't reproduce it. Those calls are
  the next thing to move off the loop, as the old AOF's final fsync
  already was.

- ✅ The result equals the keyspace at the start. A randomised test
  changes and deletes keys of all five types between slices, and separate
  tests cover expiry, eviction and `FLUSHALL` during a copy, and recovery
  from an incrementally written snapshot.
- ⚠️ Every write pays the barrier check. With no copy running, that is one
  attribute test; during a copy, a dict lookup and an integer comparison.
- ⚠️ The rewrite takes longer overall, and the old values of keys changed
  during the copy are kept until it finishes (memory).
- ⚠️ While frozen, cyclic garbage is not collected until the snapshot ends.
