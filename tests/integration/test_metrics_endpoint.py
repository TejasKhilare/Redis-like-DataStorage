"""GET /metrics on a shard, a replicated pair, and the router."""

from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest
from prometheus_client.parser import text_string_to_metric_families

import kvstore
from kvstore.core.exceptions import CommandError, WrongTypeError
from kvstore.observability.collect import shard_metrics
from kvstore.protocol.client import KVClient
from tests.helpers import make_settings, running_app
from tests.integration.test_replication import eventually, in_sync, start_node

Samples = dict[tuple[str, tuple[tuple[str, str], ...]], float]


def samples(text: str) -> Samples:
    return {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in text_string_to_metric_families(text)
        for s in family.samples
    }


def value(metrics: Samples, name: str, **labels: str) -> float:
    return metrics[(name, tuple(sorted(labels.items())))]


async def test_shard_metrics(tmp_path: Path) -> None:
    async with running_app(make_settings(tmp_path, aof_fsync="always")) as (app, http):
        async with KVClient("127.0.0.1", app.state.tcp_server.port) as client:
            for i in range(10):
                await client.execute("SET", f"k{i}", "v")
            await client.execute("GET", "k1")
            await client.execute("GET", "missing")
            with pytest.raises(WrongTypeError):
                await client.execute("LPUSH", "k1", "x")
            with pytest.raises(CommandError, match="unknown command"):  # plain -ERR on the wire
                await client.execute("FLY")

        response = await http.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
        m = samples(response.text)
        assert value(m, "kvstore_commands_total", command="set") == 10
        assert value(m, "kvstore_commands_total", command="get") == 2
        assert value(m, "kvstore_commands_total", command="unknown") == 1
        assert value(m, "kvstore_command_errors_total", command="lpush", error="WRONGTYPE") == 1
        assert value(m, "kvstore_command_duration_seconds_count", command="set") == 10
        assert value(m, "kvstore_keys") == 10
        assert value(m, "kvstore_keyspace_hits_total") == 1
        assert value(m, "kvstore_keyspace_misses_total") == 1
        assert value(m, "kvstore_aof_fsyncs_total") >= 10  # fsync=always
        assert value(m, "kvstore_aof_fsync_duration_seconds_count") >= 10
        assert value(m, "kvstore_group_commit_batch_size_count") >= 10
        assert value(m, "kvstore_aof_size_bytes") > 0
        assert value(m, "kvstore_aof_write_error") == 0
        assert value(m, "kvstore_replication_is_primary") == 1
        assert value(m, "kvstore_connected_replicas") == 0
        node_id = app.state.settings.node_id
        identity = {"node_id": node_id, "version": kvstore.__version__, "role": "primary"}
        assert value(m, "kvstore_info", **identity) == 1


async def test_replication_metrics(tmp_path: Path) -> None:
    async with AsyncExitStack() as stack:
        primary = await stack.enter_async_context(start_node(tmp_path, "p"))
        replica = await stack.enter_async_context(
            start_node(tmp_path, "r", replicaof=primary.address)
        )
        primary.run("SET", "a", "1")
        await in_sync(primary, replica)
        await eventually(lambda: bool(primary.node.primary.replicas))

        p = samples(shard_metrics(primary.node, node_id="p", group_commit=None, tcp=None))
        assert value(p, "kvstore_connected_replicas") == 1
        assert value(p, "kvstore_replication_resyncs_total", kind="full") == 1
        (lag_key,) = [k for k in p if k[0] == "kvstore_replica_lag_bytes"]
        assert p[lag_key] >= 0
        r = samples(shard_metrics(replica.node, node_id="r", group_commit=None, tcp=None))
        assert value(r, "kvstore_replication_is_primary") == 0
        assert value(r, "kvstore_replication_link_up", primary=primary.address) == 1
        assert value(r, "kvstore_replication_offset_bytes") == primary.node.state.offset


async def test_router_metrics(tmp_path: Path) -> None:
    async with AsyncExitStack() as stack:
        primary = await stack.enter_async_context(start_node(tmp_path, "p"))
        replica = await stack.enter_async_context(
            start_node(tmp_path, "r", replicaof=primary.address)
        )
        await in_sync(primary, replica)
        settings = make_settings(
            tmp_path,
            node_role="router",
            data_dir=tmp_path / "router",
            shards=f"a={primary.address}+{replica.address}",
            heartbeat_interval_s=0.05,
        )
        app, http = await stack.enter_async_context(running_app(settings))
        await eventually(lambda: app.state.manager.is_healthy(replica.address))
        async with KVClient("127.0.0.1", app.state.tcp_server.port) as client:
            await client.pipeline([["SET", f"k{i}", "v"] for i in range(20)])
            await client.execute("GET", "k1")
        m: Any = samples((await http.get("/metrics")).text)
        assert value(m, "kvstore_router_commands_total", command="set") == 20
        assert value(m, "kvstore_router_group_commands_total", shard="a") == 21
        assert value(m, "kvstore_router_group_request_duration_seconds_count", shard="a") == 2
        assert value(m, "kvstore_cluster_epoch") == 0
        node = {"address": primary.address, "shard": "a", "role": "primary"}
        assert value(m, "kvstore_cluster_node_up", **node) == 1
        replica_node = {"address": replica.address, "shard": "a", "role": "replica"}
        assert value(m, "kvstore_cluster_node_state", **replica_node, state="healthy") == 1
        lag_key = ("kvstore_cluster_replication_lag_bytes",
                   (("replica", replica.address), ("shard", "a")))  # fmt: skip
        assert lag_key in m


async def test_metrics_can_be_turned_off(tmp_path: Path) -> None:
    async with running_app(make_settings(tmp_path, metrics_enabled=False)) as (app, http):
        app.state.node.execute("SET", "a", "1")
        m = samples((await http.get("/metrics")).text)
        assert not [k for k in m if k[0] == "kvstore_commands_total"]
        assert value(m, "kvstore_keys") == 1  # read at scrape time: always there
