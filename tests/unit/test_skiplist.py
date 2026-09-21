"""Skip list + sorted set, checked against a trivially correct model (a sorted Python list)."""

import random

import pytest

from kvstore.core.exceptions import InvalidArgumentError
from kvstore.engine.datatypes import SortedSet
from kvstore.engine.datatypes.skiplist import SkipList


def check_invariants(sl: SkipList) -> None:
    """Level 0 is sorted, backward links mirror forward ones, and every span is exact."""
    nodes = list(sl)
    assert len(nodes) == sl.length
    keys = [(n.score, n.member) for n in nodes]
    assert keys == sorted(keys)
    rank = {id(node): i + 1 for i, node in enumerate(nodes)}
    rank[id(sl.header)] = 0
    for i, node in enumerate(nodes):
        assert node.backward is (nodes[i - 1] if i else None)
    assert sl.tail is (nodes[-1] if nodes else None)
    for node in [sl.header, *nodes]:
        for level in range(min(len(node.forward), sl.level)):
            target = node.forward[level]
            if target is not None:
                assert node.span[level] == rank[id(target)] - rank[id(node)]


def model_items(model: dict[str, float]) -> list[tuple[str, float]]:
    return [(m, s) for s, m in sorted((s, m) for m, s in model.items())]


def test_randomized_against_model() -> None:
    rng = random.Random(1234)
    zset, model = SortedSet(), {}
    for step in range(3000):
        member = f"m{rng.randrange(300)}"
        op = rng.random()
        if op < 0.55:
            score = float(rng.randrange(-50, 50))  # many ties: exercise member ordering
            assert zset.add(member, score) == (member not in model)
            model[member] = score
        elif op < 0.8:
            assert zset.remove(member) == (model.pop(member, None) is not None)
        else:
            delta = rng.choice([-2.5, 1.0, 3.0])
            model[member] = model.get(member, 0.0) + delta
            assert zset.incr(member, delta) == model[member]
        if step % 100 == 0:
            check_invariants(zset._list)

    check_invariants(zset._list)
    expected = model_items(model)
    assert list(zset.items()) == expected
    for index, (member, _) in enumerate(expected):
        assert zset.rank(member) == index
        assert zset.rank(member, reverse=True) == len(expected) - 1 - index


def test_range_by_rank_matches_python_slicing() -> None:
    zset = SortedSet()
    for i in range(20):
        zset.add(f"m{i:02}", float(i))
    members = [m for m, _ in zset.items()]
    for start, stop in [(0, -1), (5, 9), (-3, -1), (18, 100), (10, 5), (-100, 2), (25, 30)]:
        norm_stop = stop if stop >= 0 else len(members) + stop
        norm_start = max(0, start if start >= 0 else len(members) + start)
        expected = members[norm_start : norm_stop + 1]
        assert [n.member for n in zset.range_by_rank(start, stop)] == expected
        reversed_members = members[::-1]
        expected_rev = reversed_members[norm_start : norm_stop + 1]
        assert [n.member for n in zset.range_by_rank(start, stop, reverse=True)] == expected_rev


@pytest.mark.parametrize(
    ("lo", "hi", "lo_ex", "hi_ex", "expected"),
    [
        (2, 4, False, False, ["b", "c", "c2", "d"]),
        (2, 4, True, False, ["c", "c2", "d"]),
        (2, 4, False, True, ["b", "c", "c2"]),
        (float("-inf"), float("inf"), False, False, ["a", "b", "c", "c2", "d", "e"]),
        (3, 3, False, False, ["c", "c2"]),
        (3, 3, True, False, []),
        (6, 10, False, False, []),
        (4, 2, False, False, []),
    ],
)
def test_range_by_score(
    lo: float, hi: float, lo_ex: bool, hi_ex: bool, expected: list[str]
) -> None:
    zset = SortedSet()
    for member, score in [("a", 1), ("b", 2), ("c", 3), ("c2", 3), ("d", 4), ("e", 5)]:
        zset.add(member, float(score))
    got = zset.range_by_score(lo, hi, lo_ex=lo_ex, hi_ex=hi_ex)
    assert [n.member for n in got] == expected
    got_rev = zset.range_by_score(lo, hi, lo_ex=lo_ex, hi_ex=hi_ex, reverse=True)
    assert [n.member for n in got_rev] == expected[::-1]
    assert zset.count_in_range(lo, hi, lo_ex, hi_ex) == len(expected)


def test_range_by_score_with_limit() -> None:
    zset = SortedSet()
    for i in range(10):
        zset.add(f"m{i}", float(i))
    assert [n.member for n in zset.range_by_score(0, 9, offset=2, count=3)] == ["m2", "m3", "m4"]
    assert [n.member for n in zset.range_by_score(0, 9, reverse=True, offset=1, count=2)] == [
        "m8",
        "m7",
    ]
    assert zset.range_by_score(0, 9, offset=50) == []


def test_pop_and_nbytes_accounting() -> None:
    zset = SortedSet()
    empty = zset.nbytes
    for i in range(5):
        zset.add(f"m{i}", float(i))
    zset.add("m0", 10.0)  # re-score: no size change
    assert zset.pop(2) == [("m1", 1.0), ("m2", 2.0)]
    assert zset.pop(1, from_max=True) == [("m0", 10.0)]
    assert zset.pop(10) == [("m3", 3.0), ("m4", 4.0)]
    assert zset.nbytes == empty
    assert zset.rank("m0") is None
    check_invariants(zset._list)


def test_nan_is_rejected() -> None:
    zset = SortedSet()
    zset.add("a", float("inf"))
    with pytest.raises(InvalidArgumentError, match="NaN"):
        zset.incr("a", float("-inf"))


def test_delete_missing_and_empty_ranges() -> None:
    sl = SkipList()
    assert sl.delete(1.0, "x") is False
    assert sl.rank(1.0, "x") == 0
    assert sl.by_rank(1) is None
    assert sl.first_in_range(0, 10, False, False) is None
    assert sl.last_in_range(0, 10, False, False) is None
    sl.insert(1.0, "a")
    assert sl.delete(1.0, "b") is False
    assert sl.delete(2.0, "a") is False
