import random

from kvstore.engine.entry import Clock
from kvstore.engine.eviction.base import EvictionPolicy
from kvstore.engine.eviction.lfu import LFUPolicy
from kvstore.engine.eviction.lru import LRUPolicy
from kvstore.engine.eviction.simple import NoEvictionPolicy, RandomPolicy

POLICY_NAMES = ("lru", "lfu", "random", "noeviction")


def create_eviction_policy(name: str, *, clock: Clock, rng: random.Random) -> EvictionPolicy:
    if name == "lru":
        return LRUPolicy()
    if name == "lfu":
        return LFUPolicy(clock, rng)
    if name == "random":
        return RandomPolicy(rng)
    if name == "noeviction":
        return NoEvictionPolicy()
    raise ValueError(f"unknown eviction policy {name!r}")


__all__ = [
    "POLICY_NAMES",
    "EvictionPolicy",
    "LFUPolicy",
    "LRUPolicy",
    "NoEvictionPolicy",
    "RandomPolicy",
    "create_eviction_policy",
]
