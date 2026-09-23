# Phase 5 raw results

All measured on the machine in `environment.json` (a 2-core laptop, Ubuntu
24.04 in WSL2, Redis 7.2.7, Python 3.12.3, uvloop 0.22.1). The tables built
from these files are in [docs/BENCHMARKS.md](../../../docs/BENCHMARKS.md),
under "Phase 5: round 2".

Measuring kept finding bugs, so the files come from different commits, each
the newest code at the time:

| file | commit | what changed by then |
|---|---|---|
| `distribution.json` | `7535185` | the ring's own maths; no later commit touches it |
| `fsync-stall.json` | `210ec49` | |
| `micro.json`, `metrics-overhead.json` | `210ec49` | the leaner metrics path, and a fair A/B for it |
| `scaling.json` | `210ec49` | |
| `chaos.json` | `210ec49` | |
| `failover-default.json`, `failover-fast.json` | `b79f53a` | the kill at a random point of the heartbeat cycle |
| `pause.json` | `5370722` | the one-shot copy measured as one-shot; host stalls recorded |
| `recovery.json` | `f3b82c0` | the old AOF closed off the event loop; Redis's auto-rewrite off; `INFO` reports the copy phase |

`metrics-before-after/` holds the pairs behind two claims:

- `overhead-before.json` / `overhead-after.json` and `micro-before-*` /
  `micro-after-*`: the cost of metrics before and after commit `612173a`
  (`before` is its parent, `a8eece5`);
- `rewrite-reply.jsonl`: how long `BGREWRITEAOF` took to answer after 500k
  writes, before and after the old AOF was closed off the event loop
  (`before` is `210ec49`, `after` is `49423cb`).

Runs superseded by a fix were not kept, except as described in
BENCHMARKS.md. `environment.json` records the machine at the start of the
last full round; its `kvstore_version` is 0.4.0 because the version was
bumped to 0.5.0 after the measurements.
