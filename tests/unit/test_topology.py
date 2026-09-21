"""Cluster configuration: shard groups, the spec format, epochs, persistence."""

from pathlib import Path

import pytest

from kvstore.cluster.topology import ClusterConfig, Rebalance, ShardGroup


def test_plain_addresses_become_numbered_groups() -> None:
    config = ClusterConfig.from_spec(["127.0.0.1:6379", "127.0.0.1:6380"])
    assert [g.id for g in config.shards] == ["shard-1", "shard-2"]
    assert config.group("shard-2") == ShardGroup("shard-2", "127.0.0.1:6380")
    assert config.epoch == 0
    assert config.ring_ids == ("shard-1", "shard-2")


def test_named_groups_with_replicas() -> None:
    config = ClusterConfig.from_spec(["a=10.0.0.1:6379+10.0.0.2:6379+10.0.0.3:6379", "b=h:1"])
    group = config.group("a")
    assert group.primary == "10.0.0.1:6379"
    assert group.replicas == ("10.0.0.2:6379", "10.0.0.3:6379")
    assert group.members == ("10.0.0.1:6379", "10.0.0.2:6379", "10.0.0.3:6379")
    assert config.group_of("10.0.0.3:6379") == group
    assert config.group_of("nowhere:1") is None
    with pytest.raises(KeyError):
        config.group("c")


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ([], "at least one"),
        (["a=h:1", "a=h:2"], "unique"),
        (["h:1", "h:1"], "only one shard group"),
        (["a=h:1+h:1"], "only one shard group"),
        (["nope"], "host:port"),
        (["a=h:x"], "host:port"),
    ],
)
def test_invalid_specs(spec: list[str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ClusterConfig.from_spec(spec)


def test_ring_is_over_group_ids_so_failover_moves_no_keys() -> None:
    config = ClusterConfig.from_spec(["a=h:1+h:2", "b=h:3+h:4"])
    keys = [f"k{i}" for i in range(500)]
    before = {k: config.ring().get_node(k) for k in keys}
    promoted = config.with_group(ShardGroup("a", "h:2", ("h:1",)))
    assert promoted.epoch == config.epoch + 1
    assert promoted.group("a").primary == "h:2"
    assert {k: promoted.ring().get_node(k) for k in keys} == before


def test_rebalance_keeps_the_old_ring_until_it_finishes() -> None:
    config = ClusterConfig.from_spec(["a=h:1", "b=h:2", "c=h:3"])
    moving = config.next_epoch(rebalance=Rebalance(("a", "b")))
    assert moving.ring_ids == ("a", "b", "c")
    target = moving.target_ring()
    assert target is not None and target.nodes == ["a", "b"]
    assert config.target_ring() is None
    with pytest.raises(ValueError, match="unknown shard groups"):
        ClusterConfig(epoch=1, shards=config.shards, ring_ids=("a", "z"))


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "cluster" / "cluster.json"
    assert ClusterConfig.load(path) is None
    config = ClusterConfig.from_spec(["a=h:1+h:2", "b=h:3"], virtual_nodes=7).next_epoch(
        rebalance=Rebalance(("a",))
    )
    config.save(path)
    assert ClusterConfig.load(path) == config
    assert not path.with_name("cluster.json.tmp").exists()
