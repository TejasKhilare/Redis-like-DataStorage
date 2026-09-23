# Highlights: what this project shows, and the number behind each claim

Every line here links to the measurement or the test it comes from. Nothing
is rounded in its own favour, and where a result is noisy or unexplained, the
linked document says so.

## The work

**A Redis-compatible datastore, written from scratch in Python.** RESP2 on
the wire, five value types and about 80 commands, so unmodified `redis-cli`,
`redis-py` and `redis-benchmark` work against it — the last one is used as a
benchmark client. Sorted sets use a skip list with rank spans, a port of
Redis's `zskiplist`.
→ [README](../README.md), [ADR-0005](adr/0005-resp-and-the-value-model.md)

**Persistence modelled on Redis 7, without `fork()`.** An append-only log of
*effects* (an `EXPIRE` is logged as an absolute `PEXPIREAT`, a random `SPOP`
as the `SREM` it performed), three fsync policies, group commit across
clients, background rewrites into CRC-checked snapshots, and a manifest that
makes a crash at any point recoverable.
→ [ADR-0003](adr/0003-aof-logs-effects-not-requests.md),
[ADR-0006](adr/0006-forkless-rewrite-fsync-and-group-commit.md)

**Sharding, replication and automatic failover.** A consistent-hash ring over
shard groups, PSYNC replication with partial resync, epoch-fenced failover,
and live rebalancing that moves only the keys whose owner changes.
→ [ADR-0008](adr/0008-asynchronous-replication-with-psync.md),
[ADR-0009](adr/0009-failure-detection-and-failover-with-epochs.md),
[ADR-0010](adr/0010-rebalancing-with-ask-redirects.md)

## The numbers

| claim | measured | where |
|---|---|---|
| Failover after `kill -9`, under load | **1.74 s** median (0.47 s with fast timeouts), **0 acknowledged writes lost in 20 kills** | [BENCHMARKS](BENCHMARKS.md#failover-round-2) |
| A 6 s network partition | **0 of ~28,000** acknowledged writes lost on the majority side; the isolated primary's writes discarded on rejoin | [BENCHMARKS](BENCHMARKS.md#chaos-a-full-disk-and-a-network-partition) |
| A disk that fills up | writes refused with `MISCONF`, **100 of 100 reads served**, 0 acknowledged writes lost across a restart | same |
| Group commit across clients | **12.6–13.2 writes per fsync instead of 1**, ≈8× the write throughput under `appendfsync always` | [BENCHMARKS](BENCHMARKS.md#phase-4-before-and-after) |
| Router pipelining | **6.4×** through the router (2.0k → 13.2k ops/s) | same |
| Snapshot stalls, 1M keys | **313 ms → 38–45 ms** with an incremental copy-on-write snapshot | [BENCHMARKS](BENCHMARKS.md#what-phase-5s-own-changes-cost) |
| Recovery, 1M writes over 100k keys | **23.6 s replaying the log → 0.48 s from a snapshot** | [BENCHMARKS](BENCHMARKS.md#recovery-time-against-log-size) |
| Sharding | 3 shards do **1.8×** the pipelined work of one, until the laptop's 4 logical CPUs are full; one router caps at 1 core | [BENCHMARKS](BENCHMARKS.md#throughput-with-1-3-and-6-shards) |
| Key balance | the hottest of 3 groups carries **1.19× → 1.01×** its fair share at 100 → 500 virtual nodes | [BENCHMARKS](BENCHMARKS.md#how-evenly-keys-spread) |
| Metrics on the hot path | **~2 µs per command** (7–9% of a pipelined request's server CPU) | [ADR-0012](adr/0012-cheap-metrics-and-a-provisioned-dashboard.md) |
| Against Redis 7.2, same machine | 44% of its throughput unpipelined, ~12% CPU-bound | [BENCHMARKS](BENCHMARKS.md#summary) |

## What the testing found

Chaos tests (a fault proxy for latency and partitions, plus real processes
with a real disk limit and `kill -9`) and CI found bugs that unit tests did
not. Each one is fixed with a test that fails without the fix:

- a 100 ms latency spike failed over a **healthy** primary (the heartbeat
  timeout was tied to the heartbeat interval);
- a full disk stopped a node from serving **reads** — the failed bytes stayed
  in the buffer, so every later commit retried and failed (0 of 100 reads);
- three fsyncs ran on the event loop, freezing every client of a node: the
  cluster config during a failover, the old AOF when a rewrite starts (one
  `BGREWRITEAOF` took over 10 s; now 11–38 ms), and the manifest (still open);
- CPython's collector could stay frozen after a snapshot, so cyclic garbage
  was never collected again;
- `INFO` reported no rewrite while the keyspace was still being copied;
- two benchmarks measured the wrong thing: the failover kill was phase-locked
  to the heartbeat cycle, and the snapshot benchmark's "one-shot" copy wasn't
  one-shot after its first run.

→ [ADR-0015](adr/0015-chaos-testing-with-a-fault-proxy-and-real-processes.md),
[DESIGN](DESIGN.md#failure-modes)

## Engineering

446 tests at 95% coverage, `mypy --strict`, ruff, and CI across Python
3.11–3.13 on Linux and Windows. 15 ADRs record the decisions; every
performance claim traces to a committed result file, reproducible with the
commands in BENCHMARKS.md.

## Resume lines

Short forms, each backed by the table above:

- Built a Redis-compatible distributed key-value store in Python (RESP2,
  ~80 commands, replication, sharding, automatic failover); unmodified
  `redis-py` and `redis-benchmark` run against it.
- Cut failover to **1.74 s** with **zero acknowledged writes lost across 20
  `kill -9` runs**, and none of ~28,000 writes lost through a 6 s network
  partition.
- Removed a **313 ms** stop-the-world snapshot pause at 1M keys, replacing it
  with an incremental copy-on-write snapshot that stalls commands for
  **38–45 ms**.
- Raised write throughput **~8×** under `appendfsync always` by group
  committing across clients (**13 writes per fsync instead of 1**), and
  **6.4×** through the router by pipelining per shard group.
- Chaos-tested a real cluster (network partitions, a slow node, a disk that
  fills, `kill -9`), finding seven bugs including a spurious failover and
  three fsyncs that blocked the event loop.
- Instrumented every node with Prometheus metrics costing **~2 µs per
  command**, with a Grafana dashboard verified live during a failover.
