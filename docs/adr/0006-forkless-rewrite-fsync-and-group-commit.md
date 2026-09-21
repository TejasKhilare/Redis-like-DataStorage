# ADR-0006: Rewrite without fork(), fsync policies, and group commit

**Status:** accepted (Phase 2)

## Context
The Phase 1 AOF grew forever, called `flush()` without ever calling `fsync`,
and had no checksums. Redis bounds its AOF with `BGREWRITEAOF`, which relies
on `fork()` and copy-on-write memory. We can't do that: `fork()` doesn't
exist on Windows, and Python's reference counting touches every object, so
copy-on-write would end up copying the entire heap anyway.

## Decision
**Files.** A node's data directory holds:
- `snapshot-<g>.snap`: a binary snapshot with a CRC32;
- one or more `appendonly-<g>.aof` files, where each record also carries a CRC32;
- `manifest.json`, which lists the files that together make up the current data.

Recovery loads the snapshot, then replays the AOFs in order.

**Rewrite** (`BGREWRITEAOF`, `BGSAVE`, or automatic once the AOF has doubled
in size):
1. On the event loop, with no command running in between:
   - copy the keyspace (`Store.snapshot()`; strings are shared, collections are copied);
   - open AOF generation `g+1`;
   - record it in the manifest *next to* the current files.
2. A background thread writes the copy to `snapshot-<g+1>.snap.tmp`, fsyncs
   it, and renames it into place.
3. The next cron tick replaces the manifest with `{snapshot-g+1, [appendonly-g+1]}`
   and deletes the old files.

If the process crashes at any step, the manifest still lists a complete set
of files. A test freezes the snapshot writer halfway, copies the data
directory as a crash would leave it, and checks that nothing is lost.

**Durability** (`KV_AOF_FSYNC`):
- `always`: fsync on every commit.
- `everysec`: a background thread fsyncs once a second (the default, as in Redis).
- `no`: the OS decides when to flush.

**Group commit.** The TCP server runs every command from one socket read
inside `engine.deferred_commit()`, so the batch shares one flush/fsync, and
replies go out only afterwards. If that commit fails, the connection is
dropped rather than confirming writes that are not on disk.

**Write failures.** After an AOF write error the node refuses writes
(`MISCONF`), as Redis does, until it is restarted.

## Consequences
- ✅ Recovery time and disk usage are bounded, and a crash at any point is
  safe (covered by tests).
- ✅ `always` becomes affordable when clients pipeline: one fsync per batch
  instead of one per command.
- ⚠️ **The snapshot copy pauses the event loop for O(n)** in the number of
  keys. It is measured as `last_snapshot_pause_ms` in `INFO` and
  `/v1/admin/info`. This is the price of not having fork(), and Phase 3 will
  measure it.
- ⚠️ The serializer thread competes with the event loop for the GIL while it runs.
- ⚠️ The copy temporarily doubles the memory used by collections.
