# ADR-0008: Asynchronous primary-replica replication with PSYNC

**Status:** accepted (Phase 4)

## Context
A shard group needs copies of its data on other nodes so that it survives
losing its primary. The copies must converge to exactly the primary's state,
reconnect cheaply after short interruptions, and give the cluster manager a
way to tell which copy is the most up to date.

## Decision
**The stream is the AOF's effects.** Every write's effects (ADR-0003: `SET`,
an absolute `PEXPIREAT` for `EXPIRE`, the `SREM` a random `SPOP` performed)
are fed both to the AOF and to a replication stream of RESP arrays. Expiries
become `DEL`s in the stream, so replicas never delete a key on their own
clock: an expired key reads as missing on a replica but is only removed when
the primary's `DEL` arrives, as in Redis.

**Offsets and a backlog.** Every stream byte has an offset. The primary keeps
the last `KV_REPL_BACKLOG_BYTES` of the stream in a ring buffer. It only
creates the backlog once a replica asks for it, so a standalone node pays
nothing per write.

**The handshake is Redis's PSYNC.** A replica sends
`PSYNC <replid> <offset>`. The primary answers one of two ways:
- `+CONTINUE` plus the missing bytes, if that history and offset are still in
  its backlog;
- `+FULLRESYNC <replid> <offset>` otherwise, then a binary snapshot taken at
  exactly that offset (the Phase 2 snapshot format), then the stream.

The snapshot copy and the offset are captured with no `await` between them,
so they match exactly. The replica loads the snapshot and SAVEs at once, so
its own files never mix the old data with the new stream.

**Commit ordering.** Effects are sent to replicas at commit time, after the
AOF write. A replica therefore never holds a write that its primary could
lose on restart.

**After a promotion (PSYNC2).** A promoted replica starts a new replication
id and keeps the old one with the offset where the two histories split.
Replicas of the old primary can then continue partially from it. A former
primary that took writes nobody else received is past that point, so it must
resync fully, which discards those writes.

**Acknowledgements.** Replicas send `REPLCONF ACK <offset>` every second, and
on demand when the primary sends `REPLCONF GETACK`. These give:
- `WAIT n timeout`: block until `n` replicas have acknowledged every write so far;
- `min-replicas-to-write`: refuse writes (`-NOREPLICAS`) unless enough
  replicas acknowledged within `max-lag`;
- the replication lag in `INFO replication`.

**Roles.** A replica refuses writes with `-READONLY`. `REPLICAOF host port`
and `REPLICAOF NO ONE` switch roles at runtime, and cancel the old link
synchronously, so no stream command is applied after a promotion. Chained
replication is not supported.

## Consequences
- ✅ Replicas converge to exactly the primary's state. Tests compare the
  whole keyspace for every data type, including random `SPOP`s and TTLs.
- ✅ A short disconnect costs a partial resync, not a copy of the dataset.
- ✅ The manager can pick the most up-to-date replica by offset.
- ⚠️ **Replication is asynchronous.** A write acknowledged by the primary but
  not yet received by a replica is lost if the primary dies. The window is
  one commit plus network latency. Measured: no acknowledged write lost in
  the failover benchmark runs (see BENCHMARKS.md), but it isn't zero by
  construction. `WAIT`, or the router's write concern (ADR-0011), closes it.
- ⚠️ A full resync copies the keyspace on the event loop: the same O(n)
  pause as a rewrite (ADR-0006), 0.37 µs per key.
- ⚠️ A replica that falls further behind than the backlog, or whose unsent
  output passes 64 MB, is disconnected and resyncs fully.
