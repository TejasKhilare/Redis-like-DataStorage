"""How evenly the consistent-hash ring spreads keys, by the number of virtual nodes.

    python -m benchmarks.distribution --out benchmarks/results/phase5/distribution.json

For 3 and 6 shard groups and 10 / 100 / 500 virtual nodes per group:

* **balance** -- each group's share of 1M keys: the hottest group's share
  over the fair one (max/mean: capacity is set by the hottest shard), the
  coldest's, and the coefficient of variation. Reported for the group ids
  a cluster uses by default (``shard-1`` ...) and, since the spread depends on
  where the ids happen to hash, as the median and 95th percentile over 200
  clusters with random ids;
* **adding a group** -- the share of keys that move to a new group, against
  the ideal 1/(N+1). (Consistent hashing moves keys *only* to the new group;
  that is checked, not assumed);
* **cost** -- lookups per second and the ring's size.

No servers: this measures :class:`~kvstore.cluster.hash_ring.ConsistentHashRing`
itself. Key counts per group come from the ring's arcs over the sorted key
hashes (exact, and cross-checked against ``get_node`` on a sample).
"""

from __future__ import annotations

import argparse
import bisect
import json
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from kvstore.cluster.hash_ring import ConsistentHashRing

VNODES = (10, 100, 500)
GROUPS = (3, 6)


def key_hashes(keys: int) -> list[int]:
    return sorted(ConsistentHashRing._hash(f"key:{i}") for i in range(keys))


def counts(ring: ConsistentHashRing, hashes: list[int]) -> dict[str, int]:
    """Keys per group: a point owns the keys hashing into [previous point, point)."""
    points, owners = ring._points, ring._owners
    result = dict.fromkeys(ring.nodes, 0)
    below = [bisect.bisect_left(hashes, p) for p in points]
    for i, point in enumerate(points[1:], start=1):
        result[owners[point]] += below[i] - below[i - 1]
    # Past the last point, and before the first, wrap around to the first.
    result[owners[points[0]]] += below[0] + len(hashes) - below[-1]
    return result


def balance(per_group: dict[str, int]) -> dict[str, float]:
    values = list(per_group.values())
    mean = statistics.fmean(values)
    return {
        "max_over_mean": round(max(values) / mean, 4),
        "min_over_mean": round(min(values) / mean, 4),
        "cov": round(statistics.pstdev(values) / mean, 4),
    }


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 4)


def check_only_new_group_gains(ring: ConsistentHashRing, new: str, sample: int = 20_000) -> None:
    before = {f"key:{i}": ring.get_node(f"key:{i}") for i in range(sample)}
    ring.add_node(new)
    for key, owner in before.items():
        after = ring.get_node(key)
        if after not in (owner, new):
            raise AssertionError(f"{key} moved from {owner} to {after}, not to the new group")
    ring.remove_node(new)


def lookups_per_sec(ring: ConsistentHashRing, n: int = 200_000) -> float:
    keys = [f"key:{i}" for i in range(n)]
    get = ring.get_node
    started = time.perf_counter()
    for key in keys:
        get(key)
    return n / (time.perf_counter() - started)


def measure(hashes: list[int], groups: int, vnodes: int, clusters: int) -> dict[str, Any]:
    ids = [f"shard-{i}" for i in range(1, groups + 1)]
    ring = ConsistentHashRing(ids, virtual_nodes=vnodes)
    default = counts(ring, hashes)
    # Cross-check the arc counting against the ring's own lookups.
    sample = random.Random(0).sample(range(len(hashes)), 2_000)
    ring_hashes = ConsistentHashRing._hash
    for i in sample:
        key = f"key:{i}"
        owner = ring.get_node(key)
        h = ring_hashes(key)
        idx = bisect.bisect(ring._points, h) % len(ring._points)
        assert ring._owners[ring._points[idx]] == owner
    check_only_new_group_gains(ring, f"shard-{groups + 1}")
    grown = ConsistentHashRing([*ids, f"shard-{groups + 1}"], virtual_nodes=vnodes)
    moved = counts(grown, hashes)[f"shard-{groups + 1}"] / len(hashes)

    rng = random.Random(groups * 1000 + vnodes)
    maxes, covs, moves = [], [], []
    for _ in range(clusters):
        random_ids = [f"g-{rng.getrandbits(48):012x}" for _ in range(groups + 1)]
        ring = ConsistentHashRing(random_ids[:groups], virtual_nodes=vnodes)
        b = balance(counts(ring, hashes))
        maxes.append(b["max_over_mean"])
        covs.append(b["cov"])
        ring.add_node(random_ids[groups])
        moves.append(counts(ring, hashes)[random_ids[groups]] / len(hashes))

    return {
        "groups": groups,
        "virtual_nodes": vnodes,
        "ring_points": len(ConsistentHashRing(ids, virtual_nodes=vnodes)._points),
        "default_ids": {"keys": default, **balance(default)},
        "random_ids": {
            "clusters": clusters,
            "max_over_mean_median": _percentile(maxes, 0.5),
            "max_over_mean_p95": _percentile(maxes, 0.95),
            "cov_median": _percentile(covs, 0.5),
            "cov_p95": _percentile(covs, 0.95),
        },
        "add_one_group": {
            "ideal_share_moved": round(1 / (groups + 1), 4),
            "moved_default_ids": round(moved, 4),
            "moved_median": _percentile(moves, 0.5),
            "moved_min": round(min(moves), 4),
            "moved_max": round(max(moves), 4),
            "moved_only_to_the_new_group": True,  # check_only_new_group_gains raised otherwise
        },
        "lookups_per_sec": round(lookups_per_sec(ConsistentHashRing(ids, virtual_nodes=vnodes))),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m benchmarks.distribution", description=__doc__)
    parser.add_argument("--keys", type=int, default=1_000_000)
    parser.add_argument("--clusters", type=int, default=200)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    hashes = key_hashes(args.keys)
    rows = []
    for groups in GROUPS:
        for vnodes in VNODES:
            row = measure(hashes, groups, vnodes, args.clusters)
            rows.append(row)
            d, r, add = row["default_ids"], row["random_ids"], row["add_one_group"]
            print(
                f"{groups} groups x {vnodes:3} vnodes: max/mean {d['max_over_mean']}"
                f" (random ids: median {r['max_over_mean_median']}, p95 {r['max_over_mean_p95']})"
                f"  cov {d['cov']}  moved on +1 group {add['moved_median']}"
                f" (ideal {add['ideal_share_moved']})  {row['lookups_per_sec']:,.0f} lookups/s",
                flush=True,
            )
    report = {"keys": args.keys, "results": rows}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
