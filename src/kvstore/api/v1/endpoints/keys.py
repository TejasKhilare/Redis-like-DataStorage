"""Key-value REST API (served by both shards and the router)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from kvstore.api.deps import KVServiceDep
from kvstore.schemas.common import ErrorResponse
from kvstore.schemas.keys import (
    ExpireRequest,
    KeyTTLResponse,
    KeyTypeResponse,
    KeyValueResponse,
    SetKeyRequest,
)

router = APIRouter(
    prefix="/keys",
    tags=["keys"],
    responses={
        400: {"model": ErrorResponse, "description": "Invalid command arguments"},
        503: {"model": ErrorResponse, "description": "Owning shard unreachable (router)"},
    },
)

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Key does not exist"}
}


@router.get("/{key}", responses=_NOT_FOUND)
async def get_key(key: str, service: KVServiceDep) -> KeyValueResponse:
    """Read a string value (other types: use ``POST /v1/commands``; they answer WRONGTYPE here)."""
    return KeyValueResponse(key=key, value=await service.get(key))


@router.get("/{key}/type", responses=_NOT_FOUND)
async def get_type(key: str, service: KVServiceDep) -> KeyTypeResponse:
    return KeyTypeResponse(key=key, type=await service.key_type(key))


@router.put("/{key}")
async def set_key(key: str, body: SetKeyRequest, service: KVServiceDep) -> KeyTTLResponse:
    """Create or overwrite a key. Overwriting clears any previous TTL."""
    await service.set(key, body.value, body.ttl_seconds)
    return KeyTTLResponse(key=key, ttl_seconds=body.ttl_seconds)


@router.delete("/{key}", status_code=status.HTTP_204_NO_CONTENT, responses=_NOT_FOUND)
async def delete_key(key: str, service: KVServiceDep) -> Response:
    await service.delete(key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{key}/ttl", responses=_NOT_FOUND)
async def get_ttl(key: str, service: KVServiceDep) -> KeyTTLResponse:
    return KeyTTLResponse(key=key, ttl_seconds=await service.ttl(key))


@router.put("/{key}/ttl", responses=_NOT_FOUND)
async def set_ttl(key: str, body: ExpireRequest, service: KVServiceDep) -> KeyTTLResponse:
    await service.expire(key, body.seconds)
    return KeyTTLResponse(key=key, ttl_seconds=body.seconds)


@router.delete("/{key}/ttl", responses=_NOT_FOUND)
async def remove_ttl(key: str, service: KVServiceDep) -> KeyTTLResponse:
    """Make the key persistent (Redis ``PERSIST``)."""
    await service.persist(key)
    return KeyTTLResponse(key=key, ttl_seconds=None)
