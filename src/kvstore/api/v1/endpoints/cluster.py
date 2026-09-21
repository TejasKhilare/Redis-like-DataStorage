"""Cluster topology (router only)."""

from __future__ import annotations

from fastapi import APIRouter

from kvstore.api.deps import RouterDep
from kvstore.schemas.admin import ClusterNode, ClusterNodesResponse, KeyOwnerResponse

router = APIRouter(prefix="/cluster", tags=["cluster"])


@router.get("/nodes")
async def list_nodes(shard_router: RouterDep) -> ClusterNodesResponse:
    """Every shard with a live health probe."""
    statuses = await shard_router.node_status()
    return ClusterNodesResponse(
        virtual_nodes=shard_router.ring.virtual_nodes,
        nodes=[ClusterNode.model_validate(status) for status in statuses],
    )


@router.get("/keys/{key}/owner")
async def key_owner(key: str, shard_router: RouterDep) -> KeyOwnerResponse:
    """Which shard a key hashes to (it does not need to exist)."""
    return KeyOwnerResponse(key=key, node=shard_router.owner(key))
