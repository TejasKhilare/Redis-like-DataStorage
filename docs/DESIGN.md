# Design: the trade-offs

This is the "why" behind kvstore, and what each choice costs. The decisions
themselves are in the [ADRs](adr/); the numbers are from
[BENCHMARKS.md](BENCHMARKS.md), measured on one laptop (a 2-core i5-7200U,
Ubuntu in WSL2) against Redis 7.2 under identical conditions. Where something
is not guaranteed, this document says so.

## Contents

- [What it is, and what it isn't](#what-it-is-and-what-it-isnt)
- [One core per node](#one-core-per-node-scale-out-not-up)
- [Durability against latency](#durability-against-latency)
- [Copying a keyspace without fork()](#copying-a-keyspace-without-fork)
- [Consistency and availability when the network splits](#consistency-and-availability-when-the-network-splits)
- [A router in front, or a smarter client](#a-router-in-front-or-a-smarter-client)
- [What the instrumentation costs](#what-the-instrumentation-costs)
- [Failure modes](#failure-modes)
- [What Python costs, and where](#what-python-costs-and-where)
- [What I would change next](#what-i-would-change-next)

## What it is, and what it isn't

kvstore is a Redis-compatible, in-memory key-value store: RESP2 on the wire,
five value types, about 80 commands, an append-only file with snapshots,
primary-replica replication, automatic failover, and sharding over a
consistent-hash ring with live rebalancing. Unmodified `redis-cli`,
`redis-py` and `redis-benchmark` work against it.

It is **not** a Redis replacement. It is a study of the mechanisms behind one,
written to be measured: every performance claim in this repository comes from
a committed result file, and every failure claim from a test that fails
without its fix.

Deliberately absent: RESP3, Lua, streams, pub/sub, cluster-aware client
redirects (`MOVED`), multi-key transactions across shards, authentication,
and TLS.

## One core per node: scale out, not up

Every command runs to completion on a single event loop (ADR-0002). There are
no locks around the keyspace and no fine-grained concurrency to reason about:
a command is atomic because nothing else runs while it does. Active expiry is
an asyncio task, not the background thread that raced with the store in the
first version.

The cost is a hard ceiling of one core per node, which Python's GIL would
impose anyway. So capacity comes from more shards:

| | SET, pipelined | GET, pipelined |
|---|--:|--:|
| 1 shard | 19,376 ops/s | 49,401 ops/s |
| 3 shards | 35,372 ops/s | 89,014 ops/s |
| 6 shards | 51,216 ops/s | 96,200 ops/s |

Three shards do 1.8× the work of one, and then this laptop's four logical
CPUs are full: six shards each use about 0.65 of a core. The shape is what
matters — the ceiling is the machine, not the design.

The same property has a sharp edge: **one slow command stalls the node**, and
so does one blocking system call. Phase 5 found three places where the event
loop waited on a disk (the cluster config's fsync during a failover, the old
AOF's fsync when a rewrite starts, and the manifest's fsync, which still
does). Each one froze every client of that node, not just the caller. In a
threaded server these would have been slow requests; here they are outages.
That is the trade: simple correctness, no tolerance for blocking.

## Durability against latency

The AOF logs the *effect* of each command, after it is applied and before the
client is answered (ADR-0003), so an acknowledged write is always in the log,
and replay is deterministic — expiries are absolute, evictions are logged as
`DEL`, and nothing is logged for a command that failed.

What "durable" means is a policy, and the measured range is wide:

| `appendfsync` | writes/s (Phase 3) | what a power cut costs |
|---|--:|---|
| `always` | 406 → **1,433** with group commit across clients | nothing acknowledged |
| `everysec` | 12,679 | up to ~1 s of acknowledged writes |
| `no` | 10,710 | whatever the OS still held |

`always` is 31× slower than `everysec` here, and that gap is mostly fsync
latency on a laptop's virtual disk, not CPU. Group commit narrows it: every
connection served in one event-loop iteration shares a single write and fsync,
as Redis does in `beforeSleep`, which turned 1.0 writes per fsync into 12.6–13.2
and roughly 8× the throughput at 50 clients (ADR-0011).

Two consequences worth stating plainly:

- **`everysec` is not "almost always durable".** It is a bounded loss window,
  and the bound is real: the benchmark machine's disk needed 70 s for one
  small fsync while another process wrote in bulk.
- **A failed write is not a failed node.** When the disk filled, the node kept
  serving reads (100 of 100) and refused writes with `MISCONF` until
  restarted, and the other shard was unaffected. Getting that right took two
  fixes: after the first failure the unwritten bytes stayed in the buffer, so
  every later commit — reads included — tried to flush them again and failed.

## Copying a keyspace without fork()

Redis snapshots by forking: the child gets a frozen copy-on-write view for
free. kvstore can't. `fork()` doesn't exist on Windows, and CPython's
reference counting touches every object, so a forked child would copy the
whole heap within seconds anyway (ADR-0006).

So the keyspace is copied in the process, and the first version did it in one
pass on the event loop: 369 ms at 1M keys, with every client waiting. Phase 5
replaced it with a copy in 2 ms slices between commands, plus a copy-on-write
barrier that copies a key before a command changes it (ADR-0014). Each entry
carries the epoch of the last snapshot it was copied into, so every key is
copied exactly once and the result is the keyspace as of one instant.

| | one-shot copy | incremental |
|---|--:|--:|
| longest stall, 1M keys | 313 ms | 38–45 ms (one run of three: 392 ms) |
| longest stall, 100k hashes | 292 ms | 62–65 ms |

Measuring the remainder found something unrelated to the copy: CPython's
cycle collector walking the whole keyspace, 33–51 ms per collection,
collecting nothing, triggered by the copy's own allocations. A snapshot now
holds `gc.freeze()`, and snapshot records are tuples, which CPython stops
tracking.

What is left is honest to state: a few rewrites still stall for 212–392 ms
when the disk is busy, because the manifest's fsync and the deletion of the
old files still run on the event loop. The GC hold also has to be released on
every path, including an abandoned copy at shutdown — two leaks there kept
the collector frozen for the life of the process, and CI on Linux found them
before a user could.

## Consistency and availability when the network splits

Replication is asynchronous (ADR-0008): the primary acknowledges a write once
it is in its own AOF, and streams the effect to replicas afterwards. A
cluster manager in the router heartbeats every node and promotes the replica
with the highest offset when a primary goes silent, under a new epoch that
fences the old one (ADR-0009).

In CAP terms this is an **AP system with bounded divergence**, and the bounds
are measured rather than asserted:

| | default timeouts | with `KV_WAIT_REPLICAS=1` |
|---|--:|--:|
| failover after `kill -9` | 1.74 s (median of 5) | 1.76 s |
| clients' longest wait | 1.85 s | 6.03 s |
| acknowledged writes lost | 0 of 197,265 | 0 of 109,098 |

Under a real 6 s partition, the majority side lost none of ~28,000
acknowledged writes, the replica was promoted after 1.3 s, and the isolated
primary's writes were discarded when it rejoined 3 s after the heal.

What that does **not** mean:

- **Asynchronous replication can lose acknowledged writes.** The window is one
  commit plus network latency — sub-millisecond on loopback, which is why 20
  kills lost nothing. On a real network it is wider. `WAIT` or
  `KV_WAIT_REPLICAS=1` closes it, at a lower write rate (1,746 against 3,148
  writes/s from 20 clients in the failover setup) and a much longer outage
  (6.0 s instead of 1.85 s), because after a failover the new primary has no
  replica to wait for until the old one comes back.
- **Split brain is bounded, not prevented.** An isolated primary cannot know
  it has been replaced. Without fencing it accepted 947 writes for the whole
  partition — all discarded on rejoin. With `min-replicas-to-write 1` it
  stopped after 2.4 s, once its replica's acknowledgements aged past the lag
  limit. Clients that wrote in that window were told "OK" for writes that no
  longer exist.
- **Detection is one observer's opinion.** The manager is a single process,
  not a quorum: a partition between it and a healthy primary triggers a
  failover nobody needed. Epochs make that safe — nodes refuse commands from
  an older epoch, and the returning primary resyncs and discards its divergent
  writes — but the writes in flight are still lost. Redis Sentinel needs
  several observers to agree; Raft for the manager is the stretch goal that
  would fix both this and the manager being a single point of failure for
  failover.

The honest summary: kvstore keeps serving through a failover and never
corrupts its history, but if you need every acknowledged write to survive any
failure, you need the write concern, and you should expect the availability
cost that comes with it.

## A router in front, or a smarter client

Clients talk to a stateless router that hashes keys onto the ring and forwards
whole pipelines per shard group (ADR-0004, ADR-0011). Any number of routers
can run; the ring holds group ids, so a failover moves no keys, and adding a
group moves only the keys whose owner changes.

The cost is an extra hop, and it is the system's current ceiling:

| shards | through one router | direct to the shards |
|--:|--:|--:|
| 1 | 18,321 GETs/s | 49,401 |
| 3 | 15,110 | 89,014 |
| 6 | 12,246 | 96,200 |

One router's event loop sits at one core in every unpipelined run, and adding
groups makes it *slower*, because each client batch splits into more, smaller
per-group batches. The design answer is more routers behind a load balancer,
or a cluster-aware client that talks to shards directly — the direct column is
what such a client would get. Neither is implemented, and the table is the
reason it matters.

Two related choices:

- **Multi-key commands must stay in one group.** Otherwise the router answers
  `CROSSSLOT` rather than pretending a cross-shard `DEL` is atomic. Hash tags
  (`{user:1}:cart`) let an application keep related keys together.
- **Virtual nodes trade balance for ring size.** With the default 100 per
  group, the hottest of three groups carries 1.19× its fair share with the
  default ids — capacity is set by the hottest shard, so that is 19% wasted.
  At 500 it is 1.01×, for a ring of 1,500 points instead of 300. New clusters
  should set `KV_VIRTUAL_NODES=500`; the default stays 100 because changing it
  re-maps keys for a cluster whose router starts without a saved config.

## What the instrumentation costs

Every node exposes Prometheus metrics, and the recording sits on the hot path,
so it was built to be cheap and then measured (ADR-0012): counters are ints,
histograms are fixed buckets found with `bisect`, and everything that already
exists — key counts, memory, offsets, lag — is read only when scraped.

Recording one command costs 0.57 µs, against 3.0 µs for a labelled
`prometheus_client` histogram. With the timing around it, metrics add about
2 µs per command: 7–9% of a pipelined request's server CPU, and nothing
measurable when requests are unpipelined and cost ~75 µs each.

So the design goal was met, but the first claim — "almost nothing" — was
wrong, because it counted only the recording and not the two clock reads, the
label and the extra call around it. `KV_METRICS_ENABLED=false` gives those
7–9% back on a CPU-bound node.

## Failure modes

What happens, and what it costs, for each failure the project tests:

| failure | what happens | cost |
|---|---|---|
| primary killed | manager promotes the highest-offset replica under a new epoch; router switches at once | 1.74 s (0.47 s with fast timeouts); 0 acknowledged writes lost in 20 kills |
| primary partitioned | same, and the isolated primary is fenced on rejoin; its writes are discarded | majority side: 0 of ~28k lost; minority: up to 947 doomed writes, or 371 with `min-replicas-to-write 1` |
| old primary returns | epoch fencing demotes it; its divergent history forces a full resync | rejoined as a replica in 3 s, 20 of 20 times |
| disk fills | `MISCONF` on writes, reads keep working, other shards unaffected, no failover (the node answers) | 0 acknowledged writes lost across a `kill -9` and restart |
| AOF write fails | same as above; the node refuses writes until restarted | a torn tail is truncated on recovery (127 bytes, once) |
| node restarts | replay from the log, or load a snapshot | 23.8 s for 1M records; 7.2 s for 1M keys from a snapshot; 0.48 s when a rewrite has compacted 1M writes into 100k keys |
| replica falls behind | partial resync from the backlog, or a full resync past it | a full resync copies the keyspace (incrementally since Phase 5) |
| manager (router) dies | no failovers; routing continues from the last config in the remaining routers | unbounded until it returns — the known single point |
| disk stalls | anything still fsyncing on the event loop freezes the node | measured: a small fsync took 70 s next to a bulk writer |

## What Python costs, and where

Against Redis 7.2 on the same machine: 44% of its throughput when clients wait
for each reply, about 12% when both are CPU-bound, 15× slower to replay a log
and 7× slower to load a snapshot. A profile puts the engine itself at 19 µs
per SET and 6 µs per GET, with the rest going to RESP parsing, asyncio's
read/write path and the AOF record.

The instructive part is which gaps are the language and which were design:

- **Language.** Per-command interpreter overhead, a decoder in Python instead
  of C (about a third of the 20–27 µs it takes to replay one record), and one
  core per process.
- **Design, and fixed once measured.** Group commit across clients (18×
  behind Redis on `always`, closed to ~8× the throughput); the router
  forwarding one command at a time (6.4× when it stopped); JSON AOF records
  (0.62× the encode cost as RESP, and half that with a replica attached,
  since the same bytes now feed both); the stop-the-world snapshot copy.

None of the Phase 4 and 5 work made the interpreter faster. It removed
round trips, syscalls and duplicated encoding — which is where the wins were.

## What I would change next

In the order the measurements justify:

1. **Run several routers**, or teach the client the ring. One router is the
   ceiling (one core), and more shards make it slower.
2. **Move the manifest's fsync and the file deletions off the event loop**, as
   the old AOF's fsync already is. That is what remains of the rewrite stalls.
3. **Find the router's multi-second stalls.** Seen three times on this
   machine, once for 11.4 s, with no machine-wide freeze at the time. The
   cluster config's fsync explained one; the others are open.
4. **A decoder specialised for AOF records** (an array of bulk strings), which
   is a third of replay time.
5. **Raft for the cluster manager**, which removes both the single observer
   and the single point of failure for failover.

## Verifying any of this

- Performance claims: `benchmarks/results/` holds the raw JSON, and
  BENCHMARKS.md says which commit produced each file and how to rerun it.
- Behaviour claims: 445 tests, including chaos tests that partition a
  primary, slow it down, and fill its disk. Every bug listed here has a test
  that fails without its fix.
- The dashboard in `deploy/` was checked against a live cluster during a real
  failover: all 39 queries returned data.
