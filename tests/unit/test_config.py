from pathlib import Path

import pytest
from pydantic import ValidationError

from kvstore.core.config import Settings, get_settings


def test_defaults_derive_node_id_and_data_dir() -> None:
    settings = Settings(_env_file=None, tcp_port=6380)
    assert settings.node_id == "shard-6380"
    assert settings.data_dir == Path("data") / "shard-6380"
    assert (settings.max_keys, settings.maxmemory_bytes) == (0, 0)  # unlimited, like Redis
    assert settings.aof_fsync == "everysec"


def test_reads_prefixed_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KV_NODE_ROLE", "router")
    monkeypatch.setenv("KV_SHARDS", "a:1, b:2 ,c:3")
    monkeypatch.setenv("KV_MAXMEMORY_BYTES", "1048576")
    monkeypatch.setenv("KV_EVICTION_POLICY", "lfu")
    monkeypatch.setenv("KV_AOF_FSYNC", "always")
    monkeypatch.setenv("KV_LOG_LEVEL", "debug")

    settings = Settings(_env_file=None)

    assert settings.node_role == "router"
    assert settings.shards == ["a:1", "b:2", "c:3"]
    assert settings.maxmemory_bytes == 1_048_576
    assert settings.eviction_policy == "lfu"
    assert settings.aof_fsync == "always"
    assert settings.log_level == "DEBUG"


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_keys": -1},
        {"maxmemory_bytes": -1},
        {"http_port": 70000},
        {"shards": "no-port"},
        {"shards": "a:1,a:1"},
        {"node_role": "router", "shards": ""},
        {"eviction_policy": "volatile-ttl"},
        {"aof_fsync": "sometimes"},
    ],
)
def test_rejects_invalid_config(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    assert get_settings() is get_settings()
    get_settings.cache_clear()
