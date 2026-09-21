# ADR-0007: Benchmark methodology

**Status:** accepted (Phase 3)

## Context
Phase 3 puts numbers behind every performance claim before anything is
optimized. Those numbers are only useful if they measure the server rather
than the client or the machine, if they can be reproduced, and if the tail
latencies they report aren't flattered by how the load is driven.

## Decision
**A purpose-built load generator** (`benchmarks/load_gen.py`) instead of
redis-py or httpx. It is a bare `asyncio.Protocol` that writes pre-built
requests and only *frames* replies (it counts them and notes errors, without
decoding values). A general-purpose client spends more CPU per request than
the kvstore shard does, so it would have benchmarked itself. `redis-benchmark`
(C) runs as well, for numbers that no Python client limits, and its runs
against real Redis show the Python generator's own ceiling.

**Two load models.**
- *Closed loop* (the default) finds maximum throughput: every connection sends
  as soon as its previous reply arrives.
- *Open loop* (`--rate`) offers a fixed request rate and measures each latency
  from the request's **scheduled** send time, as wrk2 does. A closed loop hides
  stalls: a connection stuck on a slow reply stops sending, so one stall is
  recorded once instead of for every request queued behind it (coordinated
  omission). Tail latencies are quoted from the open-loop sweep, at stated
  fractions of measured capacity.

**Histograms, not sample lists or averaged percentiles.** A log-linear
histogram (128 sub-buckets per power of two, < 0.4 % error) records in O(1),
and histograms from worker processes and from repetitions merge exactly.
Averaging per-run p99s would not give a p99.

**Isolation and repetition.**
- Every run gets a freshly started server, an empty data directory and a
  preloaded keyspace (100,000 keys × 64 B), so GETs hit and no run inherits
  another's AOF.
- Each scenario runs 3 × 10 s after a 2 s warm-up. The repetitions are
  **interleaved** (rep 1 of every scenario, then rep 2, …), so drifting
  background load spreads over all scenarios instead of skewing a few.
- Reported: the median throughput with the min–max spread, and percentiles
  from the merged histograms.

**Same kernel for both servers.** kvstore and Redis 7.2 (built from source)
run inside the same Linux VM (WSL2), over loopback, with their data on the
VM's ext4 disk. On Windows, asyncio's proactor loop and a filesystem shared
with the host would penalize kvstore alone.

**Everything is recorded.** The suite writes one JSON file with the
environment (CPU, kernel, Python, event loop, Redis version, git commit,
load average), the options, every scenario's merged histogram, the open-loop
sweep and the redis-benchmark output. `benchmarks.report` rebuilds every
chart and table from that file alone.

## Consequences
- ✅ Throughput and latency claims can be traced to a committed result file
  and reproduced with `make bench REDIS=...` and `make bench-report`.
- ✅ Client-bound results are identified as such instead of being read as
  server limits.
- ⚠️ The load generator shares the CPU with the server under test. On a
  2-core laptop this caps what both can do; the numbers are comparisons under
  identical conditions, not a capacity statement for server hardware.
- ⚠️ On a machine with other work running, runs vary by up to ±30 %. The
  spread is published next to every median, and differences smaller than
  that spread are not claimed.
