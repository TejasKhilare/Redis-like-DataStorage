import json

import pytest

from kvstore.core.exceptions import (
    KVStoreError,
    NodeUnavailableError,
    ProtocolError,
    WrongArityError,
)
from kvstore.protocol.json_lines import (
    decode_request,
    decode_response,
    encode_error,
    encode_request,
    encode_result,
)


def test_request_round_trip() -> None:
    line = encode_request("SET", ["user", {"name": "tejas"}])
    assert line.endswith(b"\n")
    assert decode_request(line) == ("SET", ["user", {"name": "tejas"}])


def test_args_default_to_empty() -> None:
    assert decode_request(b'{"command": "PING"}') == ("PING", [])


@pytest.mark.parametrize(
    "line",
    [b"not json", b"[1]", b'{"args": []}', b'{"command": ""}', b'{"command": "GET", "args": "a"}'],
)
def test_bad_requests(line: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_request(line)


def test_result_round_trip() -> None:
    assert decode_response(encode_result({"a": [1, 2]})).unwrap() == {"a": [1, 2]}
    assert decode_response(encode_result(None)).unwrap() is None


def test_errors_keep_their_type_across_the_wire() -> None:
    response = decode_response(encode_error(WrongArityError("wrong number of arguments")))
    assert response.ok is False
    with pytest.raises(WrongArityError, match="wrong number of arguments"):
        response.unwrap()


def test_unknown_error_code_falls_back_to_base_class() -> None:
    line = json.dumps({"ok": False, "error": {"code": "FROM_THE_FUTURE", "message": "x"}})
    with pytest.raises(KVStoreError) as info:
        decode_response(line.encode()).unwrap()
    assert type(info.value) is KVStoreError


def test_error_registry() -> None:
    assert isinstance(KVStoreError.from_code("NODE_UNAVAILABLE", "down"), NodeUnavailableError)


@pytest.mark.parametrize(
    "line",
    [b"nope", b"[]", b'{"ok": "yes"}', b'{"ok": false, "error": "boom"}'],
)
def test_bad_responses(line: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_response(line)
