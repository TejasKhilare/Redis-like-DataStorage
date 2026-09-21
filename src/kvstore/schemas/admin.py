from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class EngineInfoSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    keys: int
    keys_with_ttl: int
    max_keys: int
    eviction_policy: str
    expired_keys: int
    evicted_keys: int
    commands_processed: int
    aof_enabled: bool
    aof_path: str | None
    aof_size_bytes: int | None
    aof_records_loaded: int
    aof_truncated_bytes: int


class NodeInfoResponse(BaseModel):
    node_id: str
    role: str
    version: str
    python_version: str
    uptime_seconds: float
    tcp_port: int | None
    engine: EngineInfoSchema | None = None
    shards: list[str] | None = None


class ClusterNode(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    address: str
    healthy: bool
    latency_ms: float | None
    error: str | None


class ClusterNodesResponse(BaseModel):
    virtual_nodes: int
    nodes: list[ClusterNode]


class KeyOwnerResponse(BaseModel):
    key: str
    node: str
