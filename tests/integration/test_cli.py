from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from kvstore.cli import format_result, main, run_command
from kvstore.core.exceptions import WrongTypeError
from kvstore.protocol.client import KVClient
from kvstore.protocol.resp import OK
from tests.helpers import make_settings, running_app


def test_format_result_like_redis_cli() -> None:
    assert format_result(None) == "(nil)"
    assert format_result(3) == "(integer) 3"
    assert format_result(OK) == "OK"
    assert format_result("tejas") == '"tejas"'
    assert format_result([]) == "(empty array)"
    assert format_result(["a", 1, None]) == '1) "a"\n2) (integer) 1\n3) (nil)'
    assert format_result([["x", "y"], "z"]) == '1) 1) "x"\n   2) "y"\n2) "z"'
    assert format_result(WrongTypeError()).startswith("(error) WRONGTYPE")
    assert format_result(1.5) == "1.5"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[KVClient]:
    async with (
        running_app(make_settings(tmp_path)) as (app, _),
        KVClient("127.0.0.1", app.state.tcp_server.port) as kv,
    ):
        yield kv


async def test_run_command(client: KVClient) -> None:
    assert await run_command(client, ["SET", "user", '{"name": "tejas"}']) == (True, "OK")
    assert await run_command(client, ["GET", "user"]) == (True, '"{\\"name\\": \\"tejas\\"}"')
    assert await run_command(client, ["GET"]) == (
        False,
        "(error) ERR wrong number of arguments for 'get' command",
    )


def test_main_one_shot_against_unreachable_node(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--port", "1", "--timeout", "0.5", "PING"]) == 1
    assert "(error) CLUSTERDOWN 127.0.0.1:1 unavailable" in capsys.readouterr().out
