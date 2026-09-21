# ADR-0002: Single-threaded command execution

**Status:** accepted (Phase 1)

## Context
The first version ran active expiry in a background **thread** that mutated the
store's dicts while the asyncio server was also using them. This caused real
races, e.g. `OrderedDict.move_to_end` on a key the other thread had just
removed.

## Decision
- All commands, and the active-expiry cycle, run **one at a time** on the event loop:
  - active expiry is an asyncio task, not a thread;
  - FastAPI endpoints are `async def` and call the engine inline, never in a thread pool.
- The engine also holds one coarse `RLock` around each command. On the event
  loop it is never contended (a few dozen ns); it makes the engine safe for
  callers that do use threads (tests, scripts).
- One node is **one process**. `python -m kvstore` passes the app object to
  uvicorn, which rules out `--workers N`: each worker would own a separate,
  diverging keyspace.

## Consequences
- ✅ Every command is atomic, and there are no fine-grained locks to reason
  about. This is the same model as Redis.
- ✅ Behaviour is deterministic, so tests are simple (a fake clock and no sleeps).
- ⚠️ One CPU core per node. We scale **out** by adding shards rather than
  **up**. With Python's GIL, threads wouldn't give parallel command execution
  anyway.
- ⚠️ A slow command blocks everything. Commands are kept O(1) or O(args), and
  each active-expiry cycle is bounded (at most 16 sampling rounds).
- ⚠️ AOF writes happen on the loop. Today each write is a buffered write plus a
  flush (microseconds); `fsync` policies in Phase 2 must not block the loop
  under `everysec`.
