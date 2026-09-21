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

## Phase 3: Benchmarks (round 1)

- [ ] Load generator: throughput, p50/p99/p999 latency
- [ ] Compare RESP with HTTP, the fsync policies, pipelining on and off, and real Redis as a baseline
- [ ] Publish `docs/BENCHMARKS.md` with charts and reproduction steps

## Phase 4: Distributed systems

- [ ] Router: forward a client's pipeline to each shard as one pipeline. Today every command
      is its own round trip: 3,000 pipelined SETs through the router took ~7 s with
      fsync=always, against ~0.1 s straight to a shard (measured on Windows during Phase 2).
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
