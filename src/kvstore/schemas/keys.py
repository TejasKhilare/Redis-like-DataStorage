from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SetKeyRequest(BaseModel):
    value: str = Field(description="A string value (store JSON as a string).", examples=["tejas"])
    ttl_seconds: int | None = Field(default=None, gt=0, description="Expire after N seconds.")


class ExpireRequest(BaseModel):
    seconds: int = Field(gt=0)


class KeyValueResponse(BaseModel):
    key: str
    value: str


class KeyTTLResponse(BaseModel):
    key: str
    ttl_seconds: int | None = Field(description="Seconds left, or null if the key never expires.")


class KeyTypeResponse(BaseModel):
    key: str
    type: str = Field(examples=["string", "list", "hash", "set", "zset"])


class CommandRequest(BaseModel):
    command: str = Field(min_length=1, examples=["ZADD"])
    args: list[str | int | float] = Field(default_factory=list, examples=[["board", 10, "tejas"]])


class CommandResponse(BaseModel):
    result: Any
