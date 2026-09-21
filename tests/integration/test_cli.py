from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from kvstore.cli import format_result, parse_token, run_command
from kvstore.protocol.client import KVClient
from tests.helpers import make_settings, running_app


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("hello", "hello"),
        ("10", "10"),
        ('{"a": 1}', {"a": 1}),
        ("[1, 2]", [1, 2]),
        ('"quoted"', "quoted"),
        ("{not json", "{not json"),
    ],
)
def test_parse_token(token: str, expected: object) -> None:
    assert parse_token(token) == expected


def test_format_result() -> None:
    assert format_result(None) == "(nil)"
    assert format_result(3) == "(integer) 3"
    assert format_result("OK") == "OK"
    assert format_result({"a": 1}) == '{\n  "a": 1\n}'


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[KVClient]:
    async with (
        running_app(make_settings(tmp_path)) as (app, _),
        KVClient("127.0.0.1", app.state.tcp_server.port) as kv,
    ):
        yield kv


async def test_run_command(client: KVClient) -> None:
    assert await run_command(client, ["SET", "user", '{"name": "tejas"}']) == (True, "OK")
    ok, output = await run_command(client, ["GET", "user"])
    assert ok
    assert '"name": "tejas"' in output
    assert await run_command(client, ["GET"]) == (
        False,
        "(error) WRONG_ARITY: wrong number of arguments for 'GET' command",
    )
