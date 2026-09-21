# ADR-0003: The AOF logs effects, not requests

**Status:** accepted (Phase 1)

## Context
Replaying the original request stream does not reproduce the original state:
- `EXPIRE k 10` means something different each time it is replayed. The
  first version logged it as an absolute `EXPIREAT`, but only after writing
  the raw request first.
- Reads aren't logged, so LRU order during replay differs from the original
  order, and re-running eviction during replay **evicts different keys**.
- Invalid commands were logged *before* validation and then replayed.

## Decision
1. **Log after applying, before replying.** A command changes memory, then
   records what it did via `propagate()`, then the client is answered. An
   acknowledged write is always in the log. A command that fails logs nothing.
2. **Log effects:**
   - relative TTLs become an absolute `PEXPIREAT` (in milliseconds);
   - a deadline already in the past becomes `DEL`;
   - each eviction is logged as `DEL victim`;
   - no-ops (`DEL` of a missing key, `PERSIST` without a TTL) are not logged.
3. **While loading, the log is the only authority.** Eviction and expiry are
   disabled during replay, as Redis does with `server.loading`. A deadline that
   has passed may be undone by a later `PERSIST` or `SET` in the log. After
   replay, expired keys are purged, and a lowered `max_keys` is enforced (and
   those evictions are logged).
4. **Crash tolerance:** a torn final record (no trailing newline, or not
   decodable) is truncated with a warning. That write was never acknowledged.
   A bad record in the middle of the file aborts startup with
   `AOFCorruptedError`, because it signals real corruption and silently
   loading partial data is worse than failing loudly.
5. **One AOF per node:** it lives at `data/<node_id>/appendonly.aof`.

## Consequences
- ✅ Replay is deterministic. Tests replay after simulated downtime and check exact state.
- ⚠️ `flush()` without `fsync` survives a process crash but not a power loss.
  Phase 2 adds `always` / `everysec` / `no`, which is the classic tradeoff
  between durability and latency.
- ⚠️ The log only grows. Phase 2 adds rewrite (compaction) and snapshots to
  bound disk usage and recovery time.
- ⚠️ If an AOF write fails (e.g. a full disk), memory and the log diverge.
  Redis refuses writes in that state; we will add that in Phase 2.
