import random

from kvstore.engine.datatypes import HashValue, ListValue, SetValue, type_name, value_size


def test_list_push_pop_and_ranges() -> None:
    items = ListValue()
    empty = items.nbytes
    items.push_right(["b", "c"])
    items.push_left(["a", "z"])  # each pushed to the head in turn: z, a, b, c
    assert list(items) == ["z", "a", "b", "c"]
    assert items.range(0, -1) == ["z", "a", "b", "c"]
    assert items.range(-2, -1) == ["b", "c"]
    assert items.range(1, 1) == ["a"]
    assert items.range(2, 100) == ["b", "c"]
    assert items.range(3, 1) == []
    assert items.range(-100, 0) == ["z"]
    assert items.get(-1) == "c"
    assert items.get(10) is None
    assert items.pop(1, left=True) == ["z"]
    assert items.pop(5, left=False) == ["c", "b", "a"]
    assert items.nbytes == empty


def test_list_tail_range_matches_head_range() -> None:
    items = ListValue(str(i) for i in range(1000))
    assert items.range(990, 995) == [str(i) for i in range(990, 996)]
    assert items.range(-5, -1) == [str(i) for i in range(995, 1000)]


def test_list_set_trim_remove() -> None:
    items = ListValue(["a", "b", "a", "c", "a"])
    assert items.set(1, "B") is True
    assert items.set(9, "x") is False
    assert items.remove(1, "a") == 1  # from the head
    assert list(items) == ["B", "a", "c", "a"]
    assert items.remove(-1, "a") == 1  # from the tail
    assert list(items) == ["B", "a", "c"]
    assert items.remove(0, "zzz") == 0
    items.trim(1, -1)
    assert list(items) == ["a", "c"]
    items.trim(5, 10)
    assert len(items) == 0


def test_hash_accounting() -> None:
    fields = HashValue()
    empty = fields.nbytes
    assert fields.set("f", "v") is True
    assert fields.set("f", "a much longer value") is False
    assert fields.get("f") == "a much longer value"
    assert "f" in fields
    assert fields.delete("f") is True
    assert fields.delete("f") is False
    assert fields.nbytes == empty


def test_set_sampling_and_accounting() -> None:
    members = SetValue()
    empty = members.nbytes
    assert members.add("a") is True
    assert members.add("a") is False
    members.add("b")
    assert sorted(members.sample(10, random.Random(0))) == ["a", "b"]
    assert members.remove("a") is True
    assert members.remove("a") is False
    members.remove("b")
    assert members.nbytes == empty


def test_type_names_and_sizes() -> None:
    assert type_name("x") == "string"
    assert type_name(ListValue()) == "list"
    assert value_size("x" * 100) > value_size("x")
