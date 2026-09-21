from collections import Counter

import pytest

from kvstore.cluster.hash_ring import ConsistentHashRing

NODES = ["10.0.0.1:6379", "10.0.0.2:6379", "10.0.0.3:6379"]
KEYS = [f"user:{i}" for i in range(20_000)]


def test_placement_is_deterministic() -> None:
    a, b = ConsistentHashRing(NODES), ConsistentHashRing(reversed(NODES))
    assert all(a.get_node(k) == b.get_node(k) for k in KEYS[:1000])


def test_keys_spread_evenly_with_virtual_nodes() -> None:
    ring = ConsistentHashRing(NODES, virtual_nodes=200)
    counts = Counter(ring.get_node(k) for k in KEYS)

    assert set(counts) == set(NODES)
    ideal = len(KEYS) / len(NODES)
    assert all(abs(n - ideal) / ideal < 0.15 for n in counts.values()), counts


def test_adding_a_node_moves_about_one_nth_of_keys() -> None:
    ring = ConsistentHashRing(NODES)
    before = {k: ring.get_node(k) for k in KEYS}
    ring.add_node("10.0.0.4:6379")
    moved = [k for k in KEYS if ring.get_node(k) != before[k]]

    # Every moved key must land on the new node -- nothing shuffles between old nodes.
    assert all(ring.get_node(k) == "10.0.0.4:6379" for k in moved)
    assert 0.15 < len(moved) / len(KEYS) < 0.35  # ideal: 1/4


def test_removing_a_node_only_moves_its_keys() -> None:
    ring = ConsistentHashRing(NODES)
    before = {k: ring.get_node(k) for k in KEYS}
    ring.remove_node("10.0.0.2:6379")

    for key in KEYS:
        if before[key] != "10.0.0.2:6379":
            assert ring.get_node(key) == before[key]
        else:
            assert ring.get_node(key) != "10.0.0.2:6379"


def test_membership() -> None:
    ring = ConsistentHashRing(NODES, virtual_nodes=10)
    assert len(ring) == 3
    assert "10.0.0.1:6379" in ring
    assert ring.nodes == sorted(NODES)

    with pytest.raises(ValueError, match="already"):
        ring.add_node("10.0.0.1:6379")
    with pytest.raises(KeyError):
        ring.remove_node("nope")


def test_empty_ring_and_bad_config() -> None:
    with pytest.raises(LookupError):
        ConsistentHashRing().get_node("k")
    with pytest.raises(ValueError, match="virtual_nodes"):
        ConsistentHashRing(virtual_nodes=0)
