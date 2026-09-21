"""Value types stored in the keyspace.

A string is a plain ``str``; the other types are small classes that keep a
running memory estimate (``nbytes``).
"""

from __future__ import annotations

from kvstore.engine.datatypes.containers import HashValue, ListValue, SetValue
from kvstore.engine.datatypes.sizing import str_size
from kvstore.engine.datatypes.zset import SortedSet

Value = str | ListValue | HashValue | SetValue | SortedSet
Collection = ListValue | HashValue | SetValue | SortedSet


def type_name(value: Value) -> str:
    return "string" if isinstance(value, str) else value.type_name


def value_size(value: Value) -> int:
    return str_size(value) if isinstance(value, str) else value.nbytes


__all__ = [
    "Collection",
    "HashValue",
    "ListValue",
    "SetValue",
    "SortedSet",
    "Value",
    "type_name",
    "value_size",
]
