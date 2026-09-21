"""Typed configuration loaded from environment variables (prefix ``KV_``) or ``.env``."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

NodeRole = Literal["shard", "router"]
EvictionPolicyName = Literal["lru", "lfu", "random", "noeviction"]
FsyncPolicyName = Literal["always", "everysec", "no"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KV_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- identity
    node_role: NodeRole = "shard"
    node_id: str = Field(default="", description="Defaults to '<role>-<tcp_port>'.")

    # ---- networking
    host: str = "127.0.0.1"
    http_port: int = Field(default=8000, ge=0, le=65535)
    tcp_port: int = Field(default=6379, ge=0, le=65535)
    tcp_enabled: bool = True
    max_request_bytes: int = Field(
        default=64 * 1024 * 1024, gt=0, description="Largest accepted bulk string."
    )

    # ---- storage engine (shard only)
    max_keys: int = Field(default=0, ge=0, description="0 = unlimited.")
    maxmemory_bytes: int = Field(default=0, ge=0, description="0 = unlimited.")
    eviction_policy: EvictionPolicyName = "lru"
    cron_interval_s: float = Field(default=0.1, gt=0, description="Housekeeping tick (hz 10).")
    active_expiry_sample_size: int = Field(default=20, gt=0)

    # ---- persistence (shard only)
    data_dir: Path | None = Field(default=None, description="Defaults to './data/<node_id>'.")
    aof_enabled: bool = True
    aof_fsync: FsyncPolicyName = "everysec"
    aof_rewrite_percentage: int = Field(default=100, ge=0, description="0 disables auto-rewrite.")
    aof_rewrite_min_bytes: int = Field(default=64 * 1024 * 1024, ge=0)

    # ---- cluster (router only)
    shards: Annotated[list[str], NoDecode] = Field(
        default=["127.0.0.1:6379", "127.0.0.1:6380", "127.0.0.1:6381"],
        description="Comma-separated 'host:port' TCP addresses of the shards.",
    )
    virtual_nodes: int = Field(default=100, gt=0)
    shard_timeout_s: float = Field(default=2.0, gt=0)

    # ---- observability
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "text"] = "json"
    access_log: bool = True

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("shards", mode="before")
    @classmethod
    def _split_shards(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("shards")
    @classmethod
    def _validate_shards(cls, value: list[str]) -> list[str]:
        for address in value:
            host, sep, port = address.rpartition(":")
            if not sep or not host or not port.isdigit():
                raise ValueError(f"shard address must be 'host:port', got {address!r}")
        if len(set(value)) != len(value):
            raise ValueError("shard addresses must be unique")
        return value

    @model_validator(mode="after")
    def _fill_defaults(self) -> Settings:
        if not self.node_id:
            self.node_id = f"{self.node_role}-{self.tcp_port}"
        if self.data_dir is None:
            self.data_dir = Path("data") / self.node_id
        if self.node_role == "router" and not self.shards:
            raise ValueError("router requires at least one shard in KV_SHARDS")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
