"""Cluster configuration: shard groups, their members, and the epoch that versions it.

A *shard group* owns a portion of the keyspace. It has one primary and
any number of replicas, and a stable id (``shard-1``) that is what the hash
ring places -- so a failover, which changes the group's primary address,
moves no keys. Only adding or removing a group changes the ring.

Every change to the configuration (a failover, a rebalance) increments the
*epoch*. A router applies only a configuration newer than the one it has,
and nodes are told the epoch they serve under, so a stale primary that
comes back can be recognised and demoted (see the manager).

``KV_SHARDS`` accepts both forms::

    127.0.0.1:6379,127.0.0.1:6380                         # groups shard-1, shard-2; no replicas
    a=127.0.0.1:6379+127.0.0.1:6479,b=127.0.0.1:6380      # named groups; '+' adds replicas
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Self

from kvstore.cluster.hash_ring import ConsistentHashRing


def _check_address(address: str) -> str:
    host, sep, port = address.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ValueError(f"node address must be 'host:port', got {address!r}")
    return address


@dataclass(frozen=True, slots=True)
class ShardGroup:
    id: str
    primary: str
    replicas: tuple[str, ...] = ()

    @property
    def members(self) -> tuple[str, ...]:
        return (self.primary, *self.replicas)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "primary": self.primary, "replicas": list(self.replicas)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(str(data["id"]), str(data["primary"]), tuple(data.get("replicas", ())))


@dataclass(frozen=True, slots=True)
class Rebalance:
    """A change of ring in progress: keys move from their old owner to their new one."""

    target: tuple[str, ...]  # the group ids of the ring being moved to

    def to_dict(self) -> dict[str, Any]:
        return {"target": list(self.target)}


@dataclass(frozen=True, slots=True)
class ClusterConfig:
    epoch: int
    shards: tuple[ShardGroup, ...]
    virtual_nodes: int = 100
    rebalance: Rebalance | None = None
    ring_ids: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        ids = [group.id for group in self.shards]
        if not ids:
            raise ValueError("a cluster needs at least one shard group")
        if len(set(ids)) != len(ids):
            raise ValueError("shard group ids must be unique")
        members = [address for group in self.shards for address in group.members]
        for address in members:
            _check_address(address)
        if len(set(members)) != len(members):
            raise ValueError("a node can belong to only one shard group, once")
        if not self.ring_ids:
            object.__setattr__(self, "ring_ids", tuple(ids))
        referenced = set(self.ring_ids) | set(self.rebalance.target if self.rebalance else ())
        unknown = referenced - set(ids)
        if unknown:
            raise ValueError(f"ring references unknown shard groups: {sorted(unknown)}")

    # ------------------------------------------------------------ lookup
    def group(self, shard_id: str) -> ShardGroup:
        for group in self.shards:
            if group.id == shard_id:
                return group
        raise KeyError(shard_id)

    def group_of(self, address: str) -> ShardGroup | None:
        for group in self.shards:
            if address in group.members:
                return group
        return None

    def ring(self) -> ConsistentHashRing:
        return ConsistentHashRing(self.ring_ids, virtual_nodes=self.virtual_nodes)

    def target_ring(self) -> ConsistentHashRing | None:
        if self.rebalance is None:
            return None
        return ConsistentHashRing(self.rebalance.target, virtual_nodes=self.virtual_nodes)

    # ----------------------------------------------------------- changes
    def with_group(self, group: ShardGroup) -> ClusterConfig:
        """This config with ``group`` replaced (same id) and the epoch bumped."""
        shards = tuple(group if g.id == group.id else g for g in self.shards)
        return replace(self, epoch=self.epoch + 1, shards=shards)

    def next_epoch(self, **changes: Any) -> ClusterConfig:
        return replace(self, epoch=self.epoch + 1, **changes)

    # ------------------------------------------------------------ (de)serialize
    def to_dict(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "virtual_nodes": self.virtual_nodes,
            "shards": [group.to_dict() for group in self.shards],
            "ring": list(self.ring_ids),
            "rebalance": self.rebalance.to_dict() if self.rebalance else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClusterConfig:
        rebalance = data.get("rebalance")
        return cls(
            epoch=int(data["epoch"]),
            virtual_nodes=int(data.get("virtual_nodes", 100)),
            shards=tuple(ShardGroup.from_dict(group) for group in data["shards"]),
            ring_ids=tuple(data.get("ring") or ()),
            rebalance=Rebalance(tuple(rebalance["target"])) if rebalance else None,
        )

    @classmethod
    def from_spec(cls, spec: Iterable[str], *, virtual_nodes: int = 100) -> ClusterConfig:
        """Parse ``KV_SHARDS`` items (see the module docstring)."""
        groups = []
        for i, item in enumerate(spec, start=1):
            shard_id, sep, members = item.partition("=")
            if not sep:
                shard_id, members = f"shard-{i}", item
            primary, *replicas = [m.strip() for m in members.split("+")]
            groups.append(ShardGroup(shard_id.strip(), primary, tuple(replicas)))
        return cls(epoch=0, shards=tuple(groups), virtual_nodes=virtual_nodes)

    # --------------------------------------------------------------- disk
    def save(self, path: Path) -> None:
        """Write atomically: a crash leaves either the old or the new config."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> ClusterConfig | None:
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
