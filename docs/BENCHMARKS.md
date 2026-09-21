# Benchmarks

Phase 3 measures kvstore before optimizing it, compares it against Redis 7.2
on the same machine, and identifies the bottlenecks. Every number here comes
from [`benchmarks/results/wsl2-i5-7200u.json`](../benchmarks/results/wsl2-i5-7200u.json);
every table is in [results.md](benchmarks/results.md). The method is described
in [ADR-0007](adr/0007-benchmark-methodology.md).

## Summary

| | kvstore | Redis 7.2 | ratio |
|---|--:|--:|--:|
| Throughput, 90% GET, no pipelining | **17,150 ops/s** | 39,139 ops/s | 0.44× |
| Latency p50 / p99, same load | 2.6 / 8.7 ms | 1.3 / 3.6 ms | |
| Throughput, pipeline depth 64 | **53,766 ops/s** | 213,894 ops/s¹ | |
| CPU-bound GETs (redis-benchmark, `-P 16`) | 60,328 ops/s | 522,739 ops/s | 0.12× |
| Writes, `appendfsync always` | 406 SETs/s | 7,389 SETs/s | **0.05×** |
| Writes, `appendfsync everysec` | 12,679 SETs/s | 40,565 SETs/s | 0.31× |

¹ Limited by the Python load generator. The C client gets 522k GETs/s from Redis.

**What the numbers show:**
1. **When clients wait for each reply, kvstore reaches 44% of Redis's
   throughput.** Round-trip overhead dominates both servers here. When both
   are CPU-bound (pipelined), kvstore does about one ninth of Redis's work,
   which is the cost of running a Python interpreter instead of C.
2. **`appendfsync always` is the biggest gap: 18× slower than Redis.** Redis
   issues one fsync per event-loop iteration for all clients together. kvstore
   group-commits only within one connection's pipeline, so 50 clients each
   pay for their own fsync.
3. **Pipelining triples kvstore's throughput** (17k → 54k ops/s), and turns
   `fsync=always` from 406 into 4,350 SETs/s.
4. **The router costs 77% of throughput** (17,150 → 3,897 ops/s), and it
   removes nearly all the benefit of pipelining (1.3× instead of 2.7×),
   because it forwards one command at a time.
5. **REST is 17× slower than RESP** (1,032 vs 17,150 ops/s). That is fine
   for a control plane (ADR-0001), but it is not a data path.
6. **A rewrite pauses the server for 0.37 µs per key.** That is 369 ms at
   1M string keys and 475 ms at 100k hashes: the price of copying the keyspace
   instead of `fork()` (ADR-0006).

## Setup

| | |
|---|---|
| Machine | Laptop, Intel Core i5-7200U (2 cores, 4 threads, 2.5 GHz), 20 GB RAM, Windows 10, "Balanced" power plan |
| Servers and client | Ubuntu 24.04 in WSL2 (4 vCPUs, 9.7 GB), Linux 6.18, loopback TCP, data on the VM's ext4 disk |
| kvstore | 0.3.0 (commit `6ad8ff7`), Python 3.12.3, uvloop 0.22.1, one shard process |
| Redis | 7.2.7 built from source, `appendonly yes`, `save ""` |
| Workload | 50 connections, 100,000 keys preloaded, 64 B values, uniform key choice |
| Runs | Fresh server per run. 2 s warm-up, then 10 s measured. 3 interleaved repetitions |

The client and the server share two physical cores, and the host had other
programs open (about 20% CPU at the start). Absolute numbers are therefore low,
and a server machine would do much better. **The comparisons are the result:**
both servers ran under the same conditions, interleaved in time. Each median
is published with its min–max spread, and no difference smaller than that
spread is claimed.

## Throughput and latency

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="benchmarks/throughput-dark.png">
  <img alt="Throughput for the same workload: Redis 39,139, kvstore RESP 17,150, kvstore via router 3,897, kvstore HTTP 1,032 ops/s" src="benchmarks/throughput-light.png">
</picture>

| server | ops/s (min–max) | p50 ms | p99 ms | p99.9 ms |
|---|--:|--:|--:|--:|
| Redis 7.2 | 39,139 (37,962–39,688) | 1.3 | 3.6 | 7.7 |
| kvstore (RESP) | 17,150 (13,949–17,669) | 2.6 | 8.7 | 17.5 |
| kvstore via router (3 shards) | 3,897 (3,815–4,061) | 8.5 | 42.1 | 70.0 |
| kvstore (HTTP, FastAPI) | 1,032 (739–1,037) | 42.9 | 138 | 232 |

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="benchmarks/latency-percentiles-dark.png">
  <img alt="Latency percentiles p50 to p99.99 for the four setups, log scale" src="benchmarks/latency-percentiles-light.png">
</picture>

kvstore's percentiles track Redis's at about 2–2.5× up to p99.9. At p99.99
kvstore reaches 644 ms (its maximum was 645 ms), while Redis's worst request
took 19 ms. That is a single stall during which all 50 clients waited. Its
cause wasn't isolated in this phase. It wasn't a rewrite: auto-rewrite needs a
64 MB AOF, far more than these runs write. Measuring GC and event-loop pauses
is part of Phase 5's observability work.

### Where the time goes

A profile of the shard under a write-only load (cProfile, in the same
environment) splits a SET's cost as follows:

- **The engine itself is cheap:** 19 µs per SET and 6 µs per GET in-process,
  or about 53k SETs/s and 173k GETs/s with no network.
- **The rest is the server path.** Parsing RESP, the asyncio stream
  read/write, and the AOF record together cost several times the engine. The
  AOF record is JSON-encoded and CRC'd for every write, then flushed once per
  batch.

That is why writes cost more than reads end to end: from 95% GETs to 5% GETs,
kvstore loses 36% of its throughput (18,161 → 11,657 ops/s). Redis stays
within its noise band (35.6k–42.1k ops/s).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="benchmarks/workload-dark.png">
  <img alt="Throughput versus share of GETs for kvstore and Redis" src="benchmarks/workload-light.png">
</picture>

## Pipelining

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="benchmarks/pipelining-dark.png">
  <img alt="Throughput versus pipeline depth 1, 4, 16, 64 for kvstore and Redis" src="benchmarks/pipelining-light.png">
</picture>

| depth | kvstore ops/s | speed-up | Redis ops/s |
|--:|--:|--:|--:|
| 1 | 17,150 | 1.0× | 39,139 |
| 4 | 33,162 | 1.9× | 99,004 |
| 16 | 46,698 | 2.7× | 161,866 |
| 64 | 53,766 | 3.1× | 213,894 |

With pipelining, fewer round trips carry the same work, and kvstore spends
the time saved on actual command execution. Its curve flattens past depth 16
because it becomes CPU-bound in the interpreter. Redis keeps climbing until
the Python load generator becomes the limit (214k ops/s). This also shows that
none of kvstore's numbers were limited by the client: its best result is a
quarter of what the same client pushed through Redis.

## Durability: fsync policies and group commit

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="benchmarks/fsync-dark.png">
  <img alt="Write throughput by appendfsync policy for kvstore and Redis" src="benchmarks/fsync-light.png">
</picture>

| appendfsync (100% SET) | kvstore SETs/s | kvstore p99 | Redis SETs/s | Redis p99 |
|---|--:|--:|--:|--:|
| always | 406 (153–434) | 3,297 ms | 7,389 (262–7,788) | 185 ms |
| everysec | 12,679 (12,652–13,382) | 8.6 ms | 40,565 (35,810–42,613) | 3.2 ms |
| no | 10,710 (8,942–11,648) | 26.1 ms | 39,595 (25,134–41,721) | 5.9 ms |
| always, pipeline 16 | 4,350 (82–4,970) | 560 ms | 75,107 (11,730–80,339) | 195 ms |

- **`always` costs kvstore 31× its `everysec` throughput, and costs Redis 5.5×.**
  Redis collects the writes of every client served in one event-loop
  iteration and fsyncs them together before replying to any of them. kvstore
  commits each connection's batch separately, so with 50 clients it runs
  up to 50 times as many fsyncs for the same writes. This is the clearest
  optimization target the benchmarks found (see below).
- **Group commit within a connection works.** Pipelining 16 SETs per round
  trip turns 406 SETs/s into 4,350 under `always`, which is 10.7×.
- **`everysec` costs nothing measurable over `no`**, because its fsync runs
  on a background thread. In this run `no` was even slightly slower, outside
  the spread. That wasn't investigated further; a likely cause is the kernel
  writing back a larger dirty page cache at unpredictable times.
- The `always` rows have wide spreads for both servers, down to 82 and 262
  SETs/s in the worst repetition. An fsync on a virtual disk backed by a
  laptop SSD sometimes takes hundreds of milliseconds.

## Latency under a fixed load

A closed loop (every client waits for its reply) measures capacity, but its
tail latencies are too optimistic. The open-loop sweep offers a fixed request
rate and measures each latency from when the request *should* have been sent.
A stall then counts against every request it delayed.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="benchmarks/latency-vs-load-dark.png">
  <img alt="p50 and p99 latency versus offered load for kvstore and Redis" src="benchmarks/latency-vs-load-light.png">
</picture>

| offered load | kvstore p50 | kvstore p99 | Redis p50 | Redis p99 |
|---|--:|--:|--:|--:|
| 20% of capacity | 1.5 ms | 78 ms | 0.75 ms | 8.9 ms |
| 60% | 1.3 ms | 18 ms | 3.6 ms | 142 ms |
| 90% | 2.1 ms | 212 ms | 4.2 ms | 429 ms |
| 100% | 866 ms | 1,262 ms | 916 ms | 2,777 ms |
| 120% | 1,405 ms | 2,391 ms | 1,055 ms | 3,448 ms |

- **The median stays flat up to 90% of capacity, then jumps about 400× at
  100%.** That is the saturation knee: past it, requests queue faster than
  they are served. The practical limit for a latency target is therefore about
  15,000 ops/s for kvstore on this machine, not the 17,150 ops/s maximum.
- **p99 on this machine is dominated by the environment, not the servers.**
  It jumps around (kvstore: 78 ms at 20% load, 18 ms at 60%), and Redis shows
  the same 100–400 ms stalls. These are VM and host scheduling hiccups, so no
  p99 figure is claimed from this laptop. Rerunning on a quiet Linux server
  (see below) is how to get one.

## BGREWRITEAOF pause

Without `fork()`, a rewrite copies the keyspace on the event loop, and no
command runs during that copy (ADR-0006). Measured in-process with
`python -m benchmarks.pause`, median of 3:

| value type | keys | pause (median / max) | background write | snapshot |
|---|--:|--:|--:|--:|
| string, 64 B | 10,000 | 2.8 / 3.5 ms | 32 ms | 1.0 MB |
| string, 64 B | 100,000 | 32.8 / 48.4 ms | 199 ms | 9.7 MB |
| string, 64 B | 1,000,000 | 369 / 442 ms | 2,076 ms | 97 MB |
| hash, 10 fields | 10,000 | 35.7 / 51.1 ms | 161 ms | 1.9 MB |
| hash, 10 fields | 100,000 | 475 / 494 ms | 1,179 ms | 19.3 MB |

The pause is linear: about **0.37 µs per string key**, and about 4.8 µs per
10-field hash (collections are copied, strings are shared). The background
write takes 2.5–11 times as long as the pause, but it doesn't block commands.
It only competes with them for the GIL. Redis's `fork()` copies page tables
instead, which the Redis documentation puts at roughly 10–20 ms per GB of
memory.

## What to optimize next

Each item is ranked by the gap it closes, measured above.

| # | Change | Evidence | Expected gain |
|--:|---|---|---|
| 1 | **Cross-client group commit:** flush the AOF once per event-loop iteration for all connections, as Redis's `beforeSleep` does | `always`: 406 vs Redis 7,389 SETs/s | Up to ~18× for `always` writes |
| 2 | **Router pipelining:** forward each client's batch as one pipeline per shard, and use connection pools instead of one serialized connection per shard | 3,897 vs 17,150 ops/s direct; P16 gains only 1.3× | 4× at depth 1, more when pipelined (already in the Phase 4 plan) |
| 3 | **Cheaper AOF records:** encode records as RESP/binary instead of JSON | Writes cost 36% of throughput; profile | Part of the SET/GET gap |
| 4 | **Incremental keyspace copy** for rewrites: copy in slices between commands, recording writes made in between | 369 ms pause at 1M keys | p99.99 tail at large keyspaces |
| 5 | Leave the REST API as is | 17× slower than RESP, by design | none (it is the control plane) |

## Reproduce

```bash
# 1. Redis 7.2 as the baseline (no root needed)
curl -fsSL https://download.redis.io/releases/redis-7.2.7.tar.gz | tar xz
make -C redis-7.2.7 -j4

# 2. kvstore with the benchmark extras (matplotlib)
pip install -e ".[dev,bench]"

# 3. The suite (~30 min), the rewrite pause (~3 min), then the charts and tables
python -m benchmarks.suite --out benchmarks/results/latest.json \
    --redis-server redis-7.2.7/src/redis-server --redis-benchmark redis-7.2.7/src/redis-benchmark
python -m benchmarks.pause --merge-into benchmarks/results/latest.json
python -m benchmarks.report benchmarks/results/latest.json

# Or: make bench REDIS=redis-7.2.7/src && make bench-report
```

Run it on Linux, with nothing else running on the machine. On Windows,
asyncio's proactor event loop and the host filesystem add costs that Redis
doesn't pay (in a quick comparison, the same load on native Windows was about 5× slower).

Single measurements:

```bash
python -m benchmarks.load_gen --port 6379                    # 50 clients, 90% GET, 10 s
python -m benchmarks.load_gen --port 6379 -P 16 --read-ratio 0
python -m benchmarks.load_gen --port 6379 --rate 10000       # open loop at 10k ops/s
python -m benchmarks.load_gen --protocol http --port 8000 --distribution zipf
```
