from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PersistenceSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    data_dir: str
    aof_fsync: str
    aof_current_size: int
    aof_base_size: int
    aof_fsyncs: int
    aof_records_loaded: int
    aof_truncated_bytes: int
    snapshot_keys_loaded: int
    load_duration_ms: float
    rewrite_in_progress: bool
    rewrites_completed: int
    rewrites_failed: int
    last_rewrite_status: str
    last_rewrite_duration_ms: float | None
    last_snapshot_pause_ms: float | None
    last_save_time: int
    write_error: str | None


class EngineInfoSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    keys: int
    keys_with_ttl: int
    max_keys: int
    maxmemory: int
    used_memory: int
    eviction_policy: str
    expired_keys: int
    evicted_keys: int
    keyspace_hits: int
    keyspace_misses: int
    commands_processed: int
    persistence: PersistenceSchema | None


class NodeInfoResponse(BaseModel):
    node_id: str
    role: str
    version: str
    python_version: str
    uptime_seconds: float
    tcp_port: int | None
    connected_clients: int | None = None
    engine: EngineInfoSchema | None = None
    shards: list[str] | None = None


class RewriteResponse(BaseModel):
    status: str


class ClusterNode(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    address: str
    shard: str
    role: str
    healthy: bool
    latency_ms: float | None
    error: str | None
    # From the cluster manager's heartbeats.
    state: str | None = None  # healthy | suspect | dead
    reported_role: str | None = None  # what the node says it is: master | slave
    offset: int | None = None
    epoch: int | None = None


class ClusterNodesResponse(BaseModel):
    epoch: int
    virtual_nodes: int
    nodes: list[ClusterNode]


class FailoverEventSchema(BaseModel):
    shard: str
    old_primary: str
    new_primary: str
    epoch: int
    reason: str
    detected_after_s: float
    promoted_at: float
    candidate_offsets: dict[str, int]


class AddShardRequest(BaseModel):
    id: str
    primary: str
    replicas: list[str] = []


class RebalanceResponse(BaseModel):
    epoch: int
    ring: list[str]
    moved_keys: int
    moved_by_shard: dict[str, int]
    duration_s: float


class KeyOwnerResponse(BaseModel):
    key: str
    shard: str
    primary: str
