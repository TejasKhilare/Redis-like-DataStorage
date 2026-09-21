from __future__ import annotations

from pydantic import BaseModel, Field, JsonValue


class SetKeyRequest(BaseModel):
    value: JsonValue = Field(
        description="Any JSON value except null.", examples=[{"name": "tejas"}]
    )
    ttl_seconds: int | None = Field(default=None, gt=0, description="Expire after N seconds.")


class ExpireRequest(BaseModel):
    seconds: int = Field(gt=0)


class KeyValueResponse(BaseModel):
    key: str
    value: JsonValue


class KeyTTLResponse(BaseModel):
    key: str
    ttl_seconds: int | None = Field(description="Seconds left, or null if the key never expires.")
