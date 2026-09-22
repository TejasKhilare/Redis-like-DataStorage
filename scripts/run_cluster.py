"""Start a local cluster -- shard groups plus one router, each node its own process.

    python scripts/run_cluster.py                  # 3 shard groups, primaries only
    python scripts/run_cluster.py --replicas 1     # ... each with a replica: failover works

Ctrl+C stops every node. Data lands in ./data/<node-id>/. Try killing a
primary: the router's cluster manager promotes its replica within ~2 s
(watch GET http://127.0.0.1:8000/v1/cluster/nodes).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

SHARD_TCP_BASE, SHARD_HTTP_BASE = 6379, 8001
REPLICA_TCP_BASE, REPLICA_HTTP_BASE = 6479, 8101
ROUTER_TCP, ROUTER_HTTP = 7000, 8000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--shards", type=int, default=3, help="shard groups")
    parser.add_argument("--replicas", type=int, default=0, help="replicas per group")
    parser.add_argument("--log-format", choices=["json", "text"], default="text")
    args = parser.parse_args()

    nodes: list[tuple[str, dict[str, str]]] = []
    groups = []
    for i in range(args.shards):
        tcp, http = SHARD_TCP_BASE + i, SHARD_HTTP_BASE + i
        primary = f"127.0.0.1:{tcp}"
        nodes.append((f"shard-{i + 1}", {"KV_TCP_PORT": str(tcp), "KV_HTTP_PORT": str(http)}))
        members = [primary]
        for r in range(args.replicas):
            offset = i * args.replicas + r
            rtcp, rhttp = REPLICA_TCP_BASE + offset, REPLICA_HTTP_BASE + offset
            members.append(f"127.0.0.1:{rtcp}")
            nodes.append(
                (
                    f"shard-{i + 1}-replica-{r + 1}",
                    {"KV_TCP_PORT": str(rtcp), "KV_HTTP_PORT": str(rhttp), "KV_REPLICAOF": primary},
                )
            )
        groups.append(f"shard-{i + 1}=" + "+".join(members))
    nodes.append(
        (
            "router",
            {
                "KV_NODE_ROLE": "router",
                "KV_TCP_PORT": str(ROUTER_TCP),
                "KV_HTTP_PORT": str(ROUTER_HTTP),
                "KV_SHARDS": ",".join(groups),
            },
        )
    )

    base_env = {**os.environ, "KV_LOG_FORMAT": args.log_format}
    processes = []
    for name, env in nodes:
        env = {**base_env, "KV_NODE_ID": name, **env}
        processes.append(subprocess.Popen([sys.executable, "-m", "kvstore"], env=env))

    print("\n  node                    tcp    http")
    for name, env in nodes:
        role = f"  (replica of {env['KV_REPLICAOF']})" if "KV_REPLICAOF" in env else ""
        print(
            f"  {name:<22}  {env['KV_TCP_PORT']:<5}  "
            f"http://127.0.0.1:{env['KV_HTTP_PORT']}/docs{role}"
        )
    print("\n  CLI:  python -m kvstore.cli --port 7000      (Ctrl+C to stop the cluster)\n")

    try:
        while all(p.poll() is None for p in processes):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for p in processes:
            if p.poll() is None:
                p.terminate()
        for p in processes:
            p.wait(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
