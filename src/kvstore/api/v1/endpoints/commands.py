"""Run any command over HTTP -- the same command table the TCP data plane serves."""

from __future__ import annotations

from fastapi import APIRouter

from kvstore.api.deps import KVServiceDep
from kvstore.schemas.common import ErrorResponse
from kvstore.schemas.keys import CommandRequest, CommandResponse

router = APIRouter(prefix="/commands", tags=["commands"])


@router.post(
    "",
    responses={
        400: {"model": ErrorResponse, "description": "Command rejected (arity, type, syntax)"},
        503: {"model": ErrorResponse, "description": "Owning shard unreachable (router)"},
    },
)
async def run_command(body: CommandRequest, service: KVServiceDep) -> CommandResponse:
    """E.g. ``{"command": "ZADD", "args": ["board", 10, "tejas"]}``.

    Arguments are sent as strings, exactly as a RESP client would.
    """
    args = [str(arg) for arg in body.args]
    return CommandResponse(result=await service.execute(body.command, *args))
