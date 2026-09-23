# Roadmap

Every phase ends with passing CI, tests for new code, and an updated README and ADRs.

## Phase 1: Foundation ✅ (v0.2.0)

- [x] `src/` layout, `pyproject.toml`, ruff, mypy `--strict`, pre-commit
- [x] Typed config (`pydantic-settings`, `KV_*` env vars, `.env`)
- [x] Structured JSON logging, request IDs, exception hierarchy with wire-stable codes
- [x] FastAPI control plane: `/v1/keys`, TTL endpoints, `/health`, `/ready`, `/v1/admin/info`, `/v1/cluster/*`
- [x] TCP data plane kept (shared by shard and router); async client; CLI
- [x] Command table: arity, write flag, key positions (used by the router)
- [x] Bug fixes:
  - [x] LRU eviction never deleted data
  - [x] every shard shared one AOF
  - [x] capacity hardcoded to 3
  - [x] invalid commands were logged to the AOF
  - [x] expiry thread raced with command execution
  - [x] replayed evictions picked different victims
  - [x] replay lost a key whose TTL was later removed (found by the new tests)
- [x] Recovery hardening: torn-tail truncation, corruption detection, no expiry/eviction while loading
- [x] 147 tests (unit and integration, fake clock), 96% coverage
- [x] Dockerfile (multi-stage, non-root, healthcheck), docker-compose cluster, GitHub Actions CI

## Phase 2: Engine depth ✅ (v0.3.0)

- [x] RESP2 on the data plane (incremental parser, inline commands, pipelining); redis-py passes its compatibility tests
- [x] AOF fsync policies: `always` / `everysec` / `no`, with group commit per pipelined batch
- [x] Background AOF rewrite without fork(): a binary CRC-checked snapshot plus a manifest that makes crashes at any point safe
- [x] CRC32 per AOF record; v1 AOFs still load
- [x] Data types: list, hash, set, sorted set (a skip list with rank spans); about 80 commands
- [x] Eviction: LFU (Redis-style decaying counters), random, noeviction (OOM), `maxmemory` in bytes
- [x] Router: hash tags, fan-out for DBSIZE, KEYS and FLUSHALL, stateless commands answered locally
- [x] MISCONF: refuse writes after an AOF write failure
- [x] 237 tests, 97% coverage, mypy --strict

## Phase 3: Benchmarks (round 1) ✅

- [x] asyncio load generator (RESP and HTTP): throughput, p50/p99/p99.9, HDR-style mergeable
      histograms, multi-process
- [x] Closed loop for capacity; open loop with coordinated-omission correction for latency under load
- [x] Suite of 25 scenarios × 3 interleaved runs: RESP vs HTTP, fsync policies, group commit,
      pipelining, read/write mix, router, real Redis 7.2 as the baseline, redis-benchmark
- [x] Rewrite pause vs keyspace size (the price of not using fork())
- [x] `docs/BENCHMARKS.md` with charts (light and dark), tables, raw results and reproduction steps
- [x] 307 tests, 97% coverage

## Phase 4: Distributed systems ✅ (v0.4.0)

From the Phase 3 bottlenecks (ADR-0011):
- [x] Router: a client's pipeline goes to each shard group as one pipeline, over multiplexed,
      pooled connections: **6.4x** with pipelining (≈2.0k → ≈13.2k ops/s)
- [x] Group commit across clients: **~13 writes per fsync instead of 1** under `appendfsync always`
- [x] Router timeouts and retries, only when a write cannot be applied twice

The plan (ADR-0008 to ADR-0010):
- [x] Shard groups (a primary plus replicas) with stable ids; a versioned config (epochs) saved as `cluster.json`
- [x] Asynchronous replication with PSYNC: full resync from a snapshot, partial resync from a
      backlog, byte offsets, acks, `WAIT`, `min-replicas-to-write`, read-only replicas
- [x] Optional reads from healthy replicas (`KV_READ_FROM_REPLICAS`)
- [x] Failure detection (healthy → suspect → dead) and automatic failover to the most
      up-to-date replica under a new epoch; a returning primary is fenced and resynced
- [x] Optional write concern: acknowledge a write only once a replica has it (`KV_WAIT_REPLICAS`)
- [x] Rebalancing: add or remove a group live, moving only the keys that change owner (≈1/N),
      with `-ASK` / `-TRYAGAIN` redirects
- [x] Cluster endpoints: nodes, config, events, failover, add/remove group
- [x] Chaos benchmark: `kill -9` a primary under load. Recovered in 1.57 s (median of 5),
      0 acknowledged writes lost, the other groups unaffected, the old primary rejoined as a replica
- [x] 358 tests, 96% coverage, mypy --strict
- [ ] Stretch, not done: Raft for the cluster config (the manager is a single process;
      see ADR-0009), N/R/W quorum reads and writes

## Phase 5: Observability and chaos testing ✅ (v0.5.0)

Carried over from the Phase 3 findings:
- [x] AOF records in RESP, encoded once for the log and the replicas (ADR-0013): a record costs
      0.62× as much to encode, 0.53× with a replica attached; replaying one is 1.47× slower
- [x] An incremental keyspace copy for rewrites and full resyncs, with CPython's cycle collector
      kept out of it (ADR-0014): most rewrites of 1M keys stall commands for 38–45 ms instead of
      313 ms; 3 runs of 15 still stalled for 212–392 ms under heavy disk writeback

Planned:
- [x] Prometheus `/metrics` on every node, about 2 µs per command (ADR-0012)
- [x] docker-compose with Prometheus 3.5 and Grafana 12.1, a provisioned 28-panel dashboard,
      checked live during a real failover
- [x] Chaos tests (ADR-0015): a fault proxy (latency, partitions) and real processes (a full
      disk, `kill -9`). In a 6 s partition the majority side lost none of ~28k acknowledged writes;
      `min-replicas-to-write 1` stopped the isolated primary after 2.4 s
- [x] Benchmarks, round 2: throughput with 1, 3 and 6 shards (shards scale to the CPUs; one
      router doesn't), key spread for 10/100/500 virtual nodes, recovery time against log size,
      failover time (1.74 s median, 0 acknowledged writes lost in 20 kills)

Found and fixed along the way, each with a test that fails without the fix:
- [x] 100 ms of latency failed over a healthy primary (the heartbeat timeout)
- [x] A full disk stopped a node from serving reads (0 of 100)
- [x] The cluster config was fsynced on the router's event loop, mid-failover
- [x] Starting a rewrite fsynced the old AOF on the event loop (`BGREWRITEAOF` took up to 669 ms,
      once over 10 s; now 11–38 ms)
- [x] CPython's collector could stay frozen after a snapshot (a second `BGREWRITEAOF`, or a node
      stopped mid-resync); found by CI on Linux
- [x] `INFO` reported no rewrite while its keyspace was still being copied
- [x] Benchmark method: the failover kill was phase-locked to the heartbeat, and the pause
      benchmark's one-shot copy wasn't one-shot after the first run
- [x] 445 tests, 95% coverage, mypy --strict

Open, from these measurements:
- [ ] Several routers, or a cluster-aware client: one router is the ceiling (1 core)
- [ ] The manifest's fsync and file deletions off the event loop (the remaining rewrite stalls)
- [ ] Multi-second stalls of the router's event loop, seen 3 times on this machine; cause unknown
- [ ] A faster AOF decoder: replay is 20–27 µs a record (15× Redis), a third of it decoding
- [ ] `KV_VIRTUAL_NODES=500` for new clusters (the hottest group: 1.19× its share → 1.01×)

## Phase 6: Write-up ✅ (v0.6.0)

- [x] [DESIGN.md](DESIGN.md): the trade-offs, each with the measurement behind it — one core
      per node, durability against latency, copying a keyspace without `fork()`, CAP under a
      partition, a router against a smarter client, what instrumentation costs, what Python
      costs, and the failure modes
- [x] `scripts/demo.py`: three groups behind a router, hash tags and `CROSSSLOT`, 5,000 writes,
      then `kill -9` of a primary — recorded as [demo.gif](demo.gif) (promoted in 2.03 s, 0
      acknowledged writes lost)
- [x] [HIGHLIGHTS.md](HIGHLIGHTS.md): resume bullets, each linked to the result that backs it
