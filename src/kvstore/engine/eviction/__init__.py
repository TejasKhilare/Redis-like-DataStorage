from kvstore.engine.eviction.base import EvictionPolicy
from kvstore.engine.eviction.lru import LRUPolicy

_POLICIES: dict[str, type[EvictionPolicy]] = {
    LRUPolicy.name: LRUPolicy,
}


def create_eviction_policy(name: str) -> EvictionPolicy:
    try:
        return _POLICIES[name]()
    except KeyError:
        raise ValueError(f"unknown eviction policy {name!r}") from None


__all__ = ["EvictionPolicy", "LRUPolicy", "create_eviction_policy"]
