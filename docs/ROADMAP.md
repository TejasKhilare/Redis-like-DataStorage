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

## Phase 4: Distributed systems

Targets from the Phase 3 numbers:
- [ ] Router: forward a client's pipeline to each shard as one pipeline. The router runs at 23% of
      a direct shard's throughput (3,897 vs 17,150 ops/s), and pipelining through it gains 1.3×
      instead of 2.7×.
- [ ] Group commit across connections: one AOF fsync per event-loop iteration, as Redis does.
      With `appendfsync always`, kvstore does 406 SETs/s against Redis's 7,389.
- [ ] Cheaper AOF record encoding (JSON today); writes cost 36% of throughput.
- [ ] Incremental keyspace copy for rewrites: the copy pauses the server 369 ms at 1M keys.
- [ ] Router connection pooling, retries, timeouts
- [ ] Primary-replica async replication with offsets
- [ ] Heartbeats, failure detection, failover with epochs (no split-brain)
- [ ] Rebalancing on node add/remove (move only affected keys)
- [ ] Stretch: Raft for cluster config; N/R/W quorums

## Phase 5: Observability and chaos testing

- [ ] Prometheus `/metrics`, Grafana dashboard
- [ ] Chaos tests: kill nodes, add latency, simulate partitions
- [ ] Benchmarks (round 2): scaling with the number of shards, failover time, recovery time

## Phase 6: Write-up

- [ ] `docs/DESIGN.md` tradeoff analysis (CAP, durability vs. latency, and more)
- [ ] Demo GIF and resume bullets backed by measured numbers
