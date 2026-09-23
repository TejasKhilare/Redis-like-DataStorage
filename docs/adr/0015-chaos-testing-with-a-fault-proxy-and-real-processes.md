# ADR-0015: Chaos testing with a fault proxy and real processes

**Status:** accepted (Phase 5)

## Context
Phase 4 tested failover with `kill -9`. A killed process is the easy
failure: its port refuses connections at once, so everyone knows. The
failures that break distributed systems are the ones where a node is
*alive but unreachable* (a partition), *alive but slow*, or *alive but
unable to write* (a full disk). They exercise different code: timeouts
instead of refused connections, fencing, and write refusal.

## Decision
**A fault proxy** (`benchmarks/faultproxy.py`, ~140 lines of asyncio). It
is placed at the address everyone uses for one primary: the router, the
cluster manager and the replica all reach it through the proxy. It can:
- **delay** every chunk by a fixed latency, per direction, keeping order;
- **partition**: forward nothing, and withhold closes too, so peers see
  timeouts, not resets, as in a real network split;
- **heal**: forward again, and reset the links that lost data, as a real
  peer would.

A client that still talks to the primary directly plays the minority side
of the partition.

*Alternatives:* `tc netem` and iptables need root (not available in WSL or
CI) and act on a whole interface, not one node's links on loopback.
Toxiproxy does the same as this proxy, but it is an external binary to
install and orchestrate; this one runs inside pytest.

**Two levels of tests.**
- `tests/integration/test_chaos.py` (in CI): partitions with and without
  `min-replicas-to-write`, 100 ms latency, a node too slow to answer, a full
  disk (a mocked `ENOSPC`).
- `python -m benchmarks.chaos`: every node its own process, and the faults
  come from the operating system. A real disk limit (`RLIMIT_FSIZE`, so
  `write()` fails with `EFBIG`), `SIGKILL`, and a partition between separate
  processes.

## Consequences
**It found four bugs, and each was fixed with a test that fails without
the fix:**
1. **A latency spike caused a failover.** The heartbeat timeout was tied to
   the heartbeat interval (0.1 s in the test), so 100 ms of latency made a
   healthy primary look dead. It is now the suspect threshold (ADR-0009).
2. **A full disk stopped a node from answering reads.** After the failed
   write, its bytes stayed in the file buffer, and every later batch,
   reads included, tried to flush them again and failed: 0 of 100 reads
   were served. Once a write has failed, the node now skips the AOF and
   refuses writes with `MISCONF`, as intended. *The mocked test had
   missed this; only the real disk limit showed it.*
3. **The manager fsynced `cluster.json` on the router's event loop,
   mid-failover.** In one run with Prometheus and Grafana, a failover took
   11.4 s instead of about 2 s, and for that time the router answered no
   client and no scrape. `benchmarks.fsync_stall` shows how that happens
   on this machine's virtual disk. Saving the file takes 9 ms idle and at
   most 44 ms next to seven AOF-like writers, but one save took 70 s next to
   an unthrottled writer. The link is inferred: two reruns didn't
   reproduce it. The router now adopts the new config at once, and the
   manager saves it in the background.
4. **The test harness itself** used two proxy addresses for one primary,
   and the manager kept re-pointing the replica at the configured one. The
   lesson carries over to deployments: the address in the cluster config
   has to be the one replicas use.

**Measured** (`benchmarks/results/phase5/chaos.json`, see BENCHMARKS.md):
- **A full disk:** the node refused writes cleanly (1 `CLUSTERDOWN` for the
  batch that failed, then `MISCONF`), served 100 of 100 reads, didn't
  disturb the other shard, and wasn't failed over. After `kill -9` and a
  restart it had lost no acknowledged write (a 127-byte torn tail was
  truncated).
- **A 6 s partition of a primary:** the replica was promoted after 1.3 s,
  the majority side lost none of its ~28k acknowledged writes, and the
  other group saw no failure. The isolated primary accepted 947 writes,
  all discarded when it rejoined 3 s after the heal. With
  `min-replicas-to-write 1` it stopped accepting after 2.4 s (371 writes).

- ⚠️ The proxy's latency is only as precise as the event loop's timers
  (about 15 ms on Windows, which one test allows for).
- ⚠️ **Not covered:** clock skew; packet loss, reordering and duplication;
  asymmetric partitions (A reaches B, B doesn't reach A); memory pressure;
  and the biggest gap, failure of the cluster manager itself, which is a
  single process (ADR-0009).
