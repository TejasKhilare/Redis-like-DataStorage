"""The load generator against a live shard: RESP, HTTP, pipelining, open loop, the CLI."""

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from benchmarks.drivers import Connection
from benchmarks.load_gen import LoadConfig, format_result, preload, run_load
from benchmarks.workload import Workload
from kvstore.engine import Engine
from tests.helpers import make_settings, running_app

ROOT = Path(__file__).resolve().parents[2]
SMALL = Workload(keyspace=200, value_size=16)


@pytest.fixture
async def shard(tmp_path: Path) -> AsyncIterator[Any]:
    async with running_app(make_settings(tmp_path)) as (app, _):
        yield app


def config(port: int, **overrides: Any) -> LoadConfig:
    values: dict[str, Any] = {
        "port": port,
        "clients": 4,
        "duration_s": 0.5,
        "warmup_s": 0.1,
        "workload": SMALL,
    }
    values.update(overrides)
    return LoadConfig(**values)


async def test_closed_loop_over_resp(shard: Any) -> None:
    port = shard.state.tcp_server.port
    await preload("127.0.0.1", port, SMALL)
    engine: Engine = shard.state.engine
    assert engine.info().keys == SMALL.keyspace

    result = await run_load(config(port))
    assert result.ops > 0
    assert result.errors == 0
    assert result.histogram.count == result.ops
    assert sum(result.timeline) <= result.ops
    assert result.ops_per_sec == pytest.approx(result.ops / result.duration_s)
    assert engine.info().keyspace_misses == 0  # the preload made every GET hit
    assert "0 errors" in format_result(result)
    data = result.to_dict()
    assert data["latency_us"]["p50"] > 0
    assert data["config"]["workload"]["keyspace"] == SMALL.keyspace


async def test_pipelined_batches_count_every_command(shard: Any) -> None:
    result = await run_load(config(shard.state.tcp_server.port, pipeline=8))
    assert result.ops > 0
    assert result.ops % 8 == 0
    assert result.errors == 0


async def test_open_loop_sends_at_the_offered_rate(shard: Any) -> None:
    result = await run_load(config(shard.state.tcp_server.port, rate=400, duration_s=1.0))
    assert result.config.mode == "open"
    assert result.errors == 0
    assert 300 <= result.ops <= 420  # 400/s for 1 s, within scheduling jitter


async def test_error_replies_are_counted(shard: Any) -> None:
    engine: Engine = shard.state.engine
    for i in range(SMALL.keyspace):  # every key holds a list: GET -> WRONGTYPE
        engine.execute("RPUSH", SMALL.key(i).decode(), "x")
    result = await run_load(config(shard.state.tcp_server.port))
    # 90% GETs of lists fail; SETs overwrite them with strings and succeed.
    assert 0 < result.errors < result.ops


async def test_http_load(tmp_path: Path) -> None:
    from kvstore.main import create_app

    app = create_app(make_settings(tmp_path, tcp_port=0))
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, access_log=False)
    )
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:  # noqa: ASYNC110 - uvicorn exposes a flag, not an event
            await asyncio.sleep(0.01)
        http_port = server.servers[0].sockets[0].getsockname()[1]
        await preload("127.0.0.1", app.state.tcp_server.port, SMALL)
        result = await run_load(config(http_port, protocol="http"))
        assert result.ops > 0
        assert result.errors == 0
        assert app.state.engine.info().keyspace_hits > 0
    finally:
        server.should_exit = True
        await task


async def test_a_dropped_connection_fails_the_request() -> None:
    async def hang_up(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(100)
        writer.close()

    server = await asyncio.start_server(hang_up, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        conn = await Connection.open("127.0.0.1", port, "resp")
        with pytest.raises(ConnectionError):
            await conn.send(b"*1\r\n$4\r\nPING\r\n", 1)
        with pytest.raises(ConnectionError, match="closed"):
            await conn.send(b"*1\r\n$4\r\nPING\r\n", 1)
        conn.close()


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ({"clients": 0}, "at least 1"),
        ({"clients": 2, "processes": 3}, "cannot exceed"),
        ({"duration_s": 0}, "must be positive"),
        ({"protocol": "http", "pipeline": 2}, "pipelining is not supported"),
        ({"rate": 100, "pipeline": 4}, "one request at a time"),
    ],
)
def test_invalid_configs_are_rejected(bad: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        config(6379, **bad)


async def test_cli_with_two_processes_writes_json(shard: Any, tmp_path: Path) -> None:
    out = tmp_path / "result.json"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "benchmarks.load_gen",
        "--port", str(shard.state.tcp_server.port),
        "--clients", "4", "--processes", "2",
        "--duration", "0.5", "--warmup", "0.1",
        "--keys", "100", "--json", str(out),
        cwd=ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    stdout, _ = await asyncio.wait_for(proc.communicate(), 60)
    assert proc.returncode == 0, stdout.decode()
    assert b"ops/s" in stdout
    data = json.loads(out.read_text())
    assert data["ops"] > 0
    assert data["errors"] == 0
    assert data["histogram"]["count"] == data["ops"]
    assert data["config"]["processes"] == 2
