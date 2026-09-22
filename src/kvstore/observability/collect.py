"""What ``GET /metrics`` exposes on a shard and on the router.

Shard: commands (rate = ops/sec, latency histograms, errors), keys and
memory, expiries and evictions, the AOF (size, fsync latency, rewrites,
write errors), group commit (connections per commit), replication (role,
offset, lag per replica, link state, resyncs) and the process.

Router: client commands, per shard group the round trips and their
latency, errors and retries, and the cluster manager's view: epoch, the
health of every node, replication lag, failovers, rebalances.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from kvstore import __version__
from kvstore.observability.metrics import Exposition, process_metrics

if TYPE_CHECKING:
    from kvstore.cluster.manager import ClusterManager
    from kvstore.cluster.router import ShardRouter
    from kvstore.protocol.tcp_server import GroupCommit, TCPServer
    from kvstore.replication.node import ShardNode


def shard_metrics(
    node: ShardNode, *, node_id: str, group_commit: GroupCommit | None, tcp: TCPServer | None
) -> str:
    out = Exposition()
    engine = node.engine
    info = engine.info()
    role = "primary" if node.role == "primary" else "replica"
    out.gauge(
        "kvstore_info",
        "Constant 1, labelled with the node's identity.",
        [({"node_id": node_id, "version": __version__, "role": role}, 1)],
    )

    stats = node.stats
    out.counter(
        "kvstore_commands",
        "Commands processed (rate = ops/sec).",
        [({"command": name}, count) for name, count in sorted(stats.calls.items())],
    )
    out.counter(
        "kvstore_command_errors",
        "Commands that answered an error, by error prefix.",
        [({"command": c, "error": e}, n) for (c, e), n in sorted(stats.errors.items())],
    )
    out.histogram(
        "kvstore_command_duration_seconds",
        "Time to execute a command on the node (excludes network and the shared commit).",
        [({"command": name}, hist) for name, hist in sorted(stats.latency.items())],
    )

    out.gauge("kvstore_keys", "Keys in the keyspace.", [({}, info.keys)])
    out.gauge("kvstore_keys_with_ttl", "Keys with an expiry.", [({}, info.keys_with_ttl)])
    out.gauge("kvstore_memory_used_bytes", "Estimated memory used by keys and values.",
              [({}, info.used_memory)])  # fmt: skip
    out.gauge("kvstore_maxmemory_bytes", "Memory limit (0 = none).", [({}, info.maxmemory)])
    out.gauge("kvstore_max_keys", "Key-count limit (0 = none).", [({}, info.max_keys)])
    out.counter("kvstore_expired_keys", "Keys removed by expiry.", [({}, info.expired_keys)])
    out.counter("kvstore_evicted_keys", "Keys removed by eviction.", [({}, info.evicted_keys)])
    out.counter("kvstore_keyspace_hits", "Reads that found their key.", [({}, info.keyspace_hits)])
    out.counter("kvstore_keyspace_misses", "Reads that did not find their key.",
                [({}, info.keyspace_misses)])  # fmt: skip

    if tcp is not None:
        out.gauge("kvstore_connected_clients", "Open RESP connections.",
                  [({}, tcp.connected_clients)])  # fmt: skip
        out.counter("kvstore_connections_received", "RESP connections accepted.",
                    [({}, tcp.connections_received)])  # fmt: skip

    p = info.persistence
    persistence = engine.persistence
    if p is not None and persistence is not None:
        out.gauge("kvstore_aof_size_bytes", "Size of the current AOF file.",
                  [({}, p.aof_current_size)])  # fmt: skip
        out.gauge("kvstore_aof_base_size_bytes", "Size of the snapshot the AOF continues.",
                  [({}, p.aof_base_size)])  # fmt: skip
        out.counter("kvstore_aof_fsyncs", "fsync calls on the AOF.", [({}, p.aof_fsyncs)])
        out.histogram("kvstore_aof_fsync_duration_seconds", "Duration of each AOF fsync.",
                      [({}, persistence.fsync_seconds)])  # fmt: skip
        out.counter(
            "kvstore_aof_rewrites",
            "Background rewrites (snapshot + new AOF), by outcome.",
            [({"status": "ok"}, p.rewrites_completed), ({"status": "failed"}, p.rewrites_failed)],
        )
        out.gauge("kvstore_aof_rewrite_in_progress", "1 while a rewrite runs.",
                  [({}, int(p.rewrite_in_progress))])  # fmt: skip
        out.gauge("kvstore_aof_write_error", "1 if an AOF write failed (writes refused).",
                  [({}, int(p.write_error is not None))])  # fmt: skip
        if p.last_snapshot_pause_ms is not None:
            out.gauge("kvstore_snapshot_pause_seconds",
                      "Event-loop pause of the last snapshot copy.",
                      [({}, p.last_snapshot_pause_ms / 1000)])  # fmt: skip

    if group_commit is not None:
        out.histogram("kvstore_group_commit_batch_size",
                      "Connections' batches that shared one AOF commit.",
                      [({}, group_commit.batch_sizes)])  # fmt: skip
        out.histogram("kvstore_group_commit_duration_seconds",
                      "Duration of each shared commit (write + fsync).",
                      [({}, group_commit.commit_seconds)])  # fmt: skip

    state, primary = node.state, node.primary
    out.gauge("kvstore_replication_is_primary", "1 on a primary, 0 on a replica.",
              [({}, int(node.role == "primary"))])  # fmt: skip
    out.gauge("kvstore_replication_offset_bytes", "Position in the replication stream.",
              [({}, state.offset)])  # fmt: skip
    out.gauge("kvstore_replication_backlog_bytes", "Bytes held for partial resyncs.",
              [({}, len(state.backlog))])  # fmt: skip
    out.gauge("kvstore_epoch", "Cluster epoch this node has seen.", [({}, node.epoch)])
    if node.role == "primary":
        replicas = list(primary.replicas.values())
        out.gauge("kvstore_connected_replicas", "Replicas streaming from this primary.",
                  [({}, len(replicas))])  # fmt: skip
        now = time.monotonic()
        out.gauge(
            "kvstore_replica_lag_bytes",
            "Stream bytes a replica has not acknowledged yet.",
            [({"replica": r.address}, max(0, state.offset - r.ack_offset)) for r in replicas],
        )
        out.gauge(
            "kvstore_replica_ack_age_seconds",
            "Seconds since a replica last acknowledged.",
            [({"replica": r.address}, now - r.ack_time) for r in replicas],
        )
        out.counter(
            "kvstore_replication_resyncs",
            "Resyncs served to replicas, by kind.",
            [
                ({"kind": "full"}, primary.full_resyncs),
                ({"kind": "partial"}, primary.partial_resyncs),
            ],
        )
    elif node.link is not None:
        link = node.link
        out.gauge("kvstore_replication_link_up", "1 while the link to the primary is up.",
                  [({"primary": link.address}, int(link.up))])  # fmt: skip
        out.gauge("kvstore_replication_last_io_age_seconds",
                  "Seconds since data last arrived from the primary.",
                  [({}, time.monotonic() - link.last_io)])  # fmt: skip

    process_metrics(out)
    return out.text()


def router_metrics(
    router: ShardRouter, *, node_id: str, manager: ClusterManager | None, tcp: TCPServer | None
) -> str:
    out = Exposition()
    out.gauge(
        "kvstore_info",
        "Constant 1, labelled with the node's identity.",
        [({"node_id": node_id, "version": __version__, "role": "router"}, 1)],
    )
    out.counter(
        "kvstore_router_commands",
        "Client commands received (rate = ops/sec).",
        [({"command": name}, n) for name, n in sorted(router.commands.items())],
    )
    out.counter(
        "kvstore_router_group_commands",
        "Commands sent to each shard group.",
        [({"shard": s}, n) for s, n in sorted(router.group_commands.items())],
    )
    stats = router.group_stats
    out.histogram(
        "kvstore_router_group_request_duration_seconds",
        "Round trip of one batch to a shard group, retries included.",
        [({"shard": s}, h) for s, h in sorted(stats.latency.items())],
    )
    out.counter(
        "kvstore_router_group_errors",
        "Commands that ended in an error, by shard group and error prefix.",
        [({"shard": s, "error": e}, n) for (s, e), n in sorted(stats.errors.items())],
    )
    out.counter("kvstore_router_retries", "Requests retried (never sent, or refused unexecuted).",
                [({}, router.retries_performed)])  # fmt: skip
    if tcp is not None:
        out.gauge("kvstore_connected_clients", "Open RESP connections.",
                  [({}, tcp.connected_clients)])  # fmt: skip

    config = router.config
    out.gauge("kvstore_cluster_epoch", "Epoch of the configuration routed by.",
              [({}, config.epoch)])  # fmt: skip
    out.gauge("kvstore_cluster_shard_groups", "Shard groups on the ring.",
              [({}, len(config.ring_ids))])  # fmt: skip
    out.gauge("kvstore_cluster_rebalance_in_progress", "1 while keys are being moved.",
              [({}, int(config.rebalance is not None))])  # fmt: skip
    if manager is not None:
        up: list[tuple[dict[str, str], float]] = []
        states: list[tuple[dict[str, str], float]] = []
        lag: list[tuple[dict[str, str], float]] = []
        for group in config.shards:
            primary = manager.health.get(group.primary)
            for address in group.members:
                health = manager.health.get(address)
                role = "primary" if address == group.primary else "replica"
                labels = {"address": address, "shard": group.id, "role": role}
                state = health.state if health else "unknown"
                up.append((labels, int(state == "healthy")))
                states.extend(
                    ({**labels, "state": s}, int(state == s))
                    for s in ("healthy", "suspect", "dead")
                )
                if role == "replica" and health and primary and health.state == "healthy":
                    lag.append(({"shard": group.id, "replica": address},
                                max(0, primary.offset - health.offset)))  # fmt: skip
        out.gauge("kvstore_cluster_node_up", "1 if the manager's last heartbeat succeeded.", up)
        out.gauge("kvstore_cluster_node_state", "The manager's view of each node (1 = current).",
                  states)  # fmt: skip
        out.gauge("kvstore_cluster_replication_lag_bytes",
                  "Primary offset minus replica offset, from the heartbeats.", lag)  # fmt: skip
        failovers: dict[str, int] = {}
        for event in manager.events:
            failovers[event.shard] = failovers.get(event.shard, 0) + 1
        out.counter("kvstore_cluster_failovers", "Failovers, by shard group.",
                    [({"shard": s}, n) for s, n in sorted(failovers.items())])  # fmt: skip
    process_metrics(out)
    return out.text()
