"""Start a local cluster -- N shards plus one router, each in its own process.

    python scripts/run_cluster.py            # 3 shards
    python scripts/run_cluster.py --shards 5

Ctrl+C stops every node. Data lands in ./data/<node-id>/.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

SHARD_TCP_BASE, SHARD_HTTP_BASE = 6379, 8001
ROUTER_TCP, ROUTER_HTTP = 7000, 8000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--shards", type=int, default=3)
    parser.add_argument("--log-format", choices=["json", "text"], default="text")
    args = parser.parse_args()

    nodes: list[tuple[str, dict[str, str]]] = []
    shard_addresses = []
    for i in range(args.shards):
        tcp, http = SHARD_TCP_BASE + i, SHARD_HTTP_BASE + i
        shard_addresses.append(f"127.0.0.1:{tcp}")
        nodes.append((f"shard-{i + 1}", {"KV_TCP_PORT": str(tcp), "KV_HTTP_PORT": str(http)}))
    nodes.append(
        (
            "router",
            {
                "KV_NODE_ROLE": "router",
                "KV_TCP_PORT": str(ROUTER_TCP),
                "KV_HTTP_PORT": str(ROUTER_HTTP),
                "KV_SHARDS": ",".join(shard_addresses),
            },
        )
    )

    base_env = {**os.environ, "KV_LOG_FORMAT": args.log_format}
    processes = []
    for name, env in nodes:
        env = {**base_env, "KV_NODE_ID": name, **env}
        processes.append(subprocess.Popen([sys.executable, "-m", "kvstore"], env=env))

    print("\n  node       tcp    http")
    for name, env in nodes:
        print(f"  {name:<9}  {env['KV_TCP_PORT']:<5}  http://127.0.0.1:{env['KV_HTTP_PORT']}/docs")
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
