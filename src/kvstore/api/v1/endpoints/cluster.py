"""Cluster topology, health and failover (router only)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from kvstore.api.deps import ManagerDep, RouterDep
from kvstore.cluster.topology import ShardGroup
from kvstore.core.exceptions import InvalidArgumentError
from kvstore.schemas.admin import (
    AddShardRequest,
    ClusterNode,
    ClusterNodesResponse,
    FailoverEventSchema,
    KeyOwnerResponse,
    RebalanceResponse,
)

router = APIRouter(prefix="/cluster", tags=["cluster"])


@router.get("/nodes")
async def list_nodes(shard_router: RouterDep, manager: ManagerDep) -> ClusterNodesResponse:
    """Every node: a live probe from the router, plus the manager's view of it."""
    statuses = await shard_router.node_status()
    nodes = []
    for status in statuses:
        node = ClusterNode.model_validate(status)
        health = manager.health.get(status.address)
        if health is not None:
            node.state, node.reported_role = health.state, health.role
            node.offset, node.epoch = health.offset, health.epoch
        nodes.append(node)
    return ClusterNodesResponse(
        epoch=shard_router.config.epoch,
        virtual_nodes=shard_router.ring.virtual_nodes,
        nodes=nodes,
    )


@router.get("/config")
async def cluster_config(shard_router: RouterDep) -> dict[str, Any]:
    """The configuration the router routes by (it is saved as cluster.json)."""
    return shard_router.config.to_dict()


@router.get("/events")
async def failover_events(manager: ManagerDep) -> list[FailoverEventSchema]:
    """Every failover so far, oldest first."""
    return [FailoverEventSchema(**event.to_dict()) for event in manager.events]


@router.post("/shards/{shard}/failover")
async def failover(shard: str, manager: ManagerDep) -> FailoverEventSchema:
    """Promote the most up-to-date replica of ``shard`` (after letting it catch up)."""
    event = await manager.failover(shard)
    return FailoverEventSchema(**event.to_dict())


@router.post("/shards")
async def add_shard(body: AddShardRequest, manager: ManagerDep) -> RebalanceResponse:
    """Add a shard group (start its nodes first; the primary should be empty).

    Returns once its share of the keys -- about 1/N -- has been moved to it.
    """
    try:
        group = ShardGroup(body.id, body.primary, tuple(body.replicas))
        result = await manager.add_group(group)
    except ValueError as exc:
        raise InvalidArgumentError(str(exc)) from None
    return RebalanceResponse(**result)


@router.delete("/shards/{shard}")
async def remove_shard(shard: str, manager: ManagerDep) -> RebalanceResponse:
    """Move every key off ``shard``, then remove it from the cluster."""
    try:
        result = await manager.remove_group(shard)
    except KeyError:
        raise InvalidArgumentError(f"unknown shard group {shard!r}") from None
    return RebalanceResponse(**result)


@router.get("/keys/{key}/owner")
async def key_owner(key: str, shard_router: RouterDep) -> KeyOwnerResponse:
    """Which shard group a key hashes to (it does not need to exist), and its primary."""
    shard = shard_router.owner(key)
    return KeyOwnerResponse(key=key, shard=shard, primary=shard_router.primary(shard))
