# ADR-0012: Cheap per-command metrics, and a provisioned dashboard

**Status:** accepted (Phase 5)

## Context
Until Phase 4, everything was measured from outside, with the load
generator and `INFO`. Operating a cluster needs throughput, latency,
memory, replication lag and failovers visible while they happen.
Prometheus's pull model is the standard way to get that. But the
recording happens on every command, in a Python process where the engine's
own work for a `SET` is about 20 µs, so the instrumentation must not become
a noticeable part of that.

## Decision
**`GET /metrics` on every node**, in the Prometheus text format (0.0.4),
with a shard and a router variant.

**Record on the hot path only what exists nowhere else**, in plain
Python:
- per command: a counter, an error counter by error prefix, and a latency
  histogram (fixed buckets from 10 µs to 2.5 s, found with `bisect`);
- connections per group commit, and the commit's duration;
- AOF fsync durations;
- the router's round trips to each shard group, with their errors and retries;
- CPython's collector pauses, by generation (via `gc.callbacks`).

**Read everything else when scraped**: key counts, memory, AOF sizes,
replication offsets, per-replica lag and ack age, the manager's view of
every node, and failovers. None of it is copied on the hot path.

`KV_METRICS_ENABLED=false` turns recording off.

**Not `prometheus_client` on the hot path.** A labelled histogram
observation costs four to five times as much as kvstore's recording (below).
The library is still used in the tests, to parse `/metrics` with the
official parser.

**A dashboard in the repository.** `docker compose up` starts three shard
groups (a primary and a replica each), the router, Prometheus 3.5 and
Grafana 12.1. Grafana is provisioned with the datasource and a dashboard
(`deploy/grafana/dashboards/kvstore.json`: 28 panels in 6 rows, 39
queries). A CI test checks
that every query uses a metric the collectors can emit, and that every
node in the compose file is scraped.

## Consequences
**Measured** (WSL2; details in BENCHMARKS.md, "What Phase 5's own changes
cost"):
- Recording a command costs 0.57 µs, against 3.0 µs for a labelled
  `prometheus_client` histogram.
- With the timing around it (two clock reads, the label, the extra call),
  metrics add about 2 µs per command. With 16-deep pipelines that was
  2.1–2.6 µs more server CPU per request (+7–9%) in two comparisons, and
  5% of the samples in a profile. A third comparison, on a slower spell of
  the machine, stayed inside its noise. Unpipelined, the cost is lost in
  the ~75 µs a request takes.
- ⚠️ **So the metrics are cheap, not free.** The first estimate, 0.8 µs per
  command, counted only the recording. Trimming the path (one histogram
  instead of a histogram plus a counter, and the label from one lookup)
  moved the measured cost by less than the noise.
  `KV_METRICS_ENABLED=false` gives the 7–9% back to a CPU-bound node.

- ✅ **Checked live once**, on the same pinned versions without Docker
  (each node on its own loopback IP, as containers get their own host):
  `promtool` accepts the config, and all 39 queries return data through
  Grafana during a real `SIGKILL` failover. That run found two dashboard
  bugs a static check can't: value mappings ignored because of an explicit
  color mode, and a node shown twice after its `role` label changed.
- ✅ The dashboard showed what the load generator never did: fsync p99
  above 2.5 s on the laptop's virtual disk, and a failover during which the
  router's own scrapes went missing (ADR-0015).
- ⚠️ The exposition is ~200 lines of our own code instead of a library.
  Tests parse its output with `prometheus_client`'s parser.
- ⚠️ Label cardinality is bounded by design: commands outside the command
  table are counted as `unknown`, and replicas are labelled by address.
- ⚠️ No alerting rules are shipped. The dashboard is for looking, not paging.
