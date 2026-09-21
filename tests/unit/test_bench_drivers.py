"""Reply framing and request building in the load generator's wire clients."""

import json

import pytest

from benchmarks.drivers import (
    FramingError,
    RequestEncoder,
    Scanner,
    http_get,
    http_put,
    resp_command,
    scan_http,
    scan_resp,
)
from kvstore.protocol.resp import RequestParser


def frames(scanner: Scanner, data: bytes) -> list[tuple[int, bool]]:
    buf, pos, out = bytearray(data), 0, []
    while (frame := scanner(buf, pos)) is not None:
        out.append(frame)
        pos = frame[0]
    return out


@pytest.mark.parametrize(
    ("reply", "is_error"),
    [
        (b"+OK\r\n", False),
        (b"-ERR nope\r\n", True),
        (b":42\r\n", False),
        (b"$5\r\nhello\r\n", False),
        (b"$-1\r\n", False),
        (b"*-1\r\n", False),
        (b"*0\r\n", False),
        (b"*2\r\n$1\r\na\r\n*1\r\n:1\r\n", False),
        (b"*2\r\n+OK\r\n-WRONGTYPE bad\r\n", True),
    ],
)
def test_scan_resp_frames_each_reply_type(reply: bytes, is_error: bool) -> None:
    assert scan_resp(bytearray(reply), 0) == (len(reply), is_error)


def test_scan_resp_waits_for_incomplete_replies() -> None:
    whole = b"*2\r\n$5\r\nhello\r\n$5\r\nworld\r\n"
    for cut in range(len(whole)):
        assert scan_resp(bytearray(whole[:cut]), 0) is None
    assert scan_resp(bytearray(whole), 0) == (len(whole), False)


def test_scan_resp_frames_back_to_back_replies() -> None:
    data = b"+OK\r\n$3\r\nabc\r\n-ERR x\r\n$-1\r\n"
    assert [error for _, error in frames(scan_resp, data)] == [False, False, True, False]


def test_scan_resp_rejects_garbage() -> None:
    with pytest.raises(FramingError):
        scan_resp(bytearray(b"?what\r\n"), 0)


def test_resp_command_is_what_the_server_parses() -> None:
    parser = RequestParser()
    parser.feed(resp_command(b"SET", b"key:1", b"x" * 64))
    assert parser.next_command() == ["SET", "key:1", "x" * 64]


def response(status: int, body: bytes = b"", *, reason: bytes = b"OK") -> bytes:
    return b"HTTP/1.1 %d %s\r\ncontent-type: application/json\r\ncontent-length: %d\r\n\r\n%s" % (
        status,
        reason,
        len(body),
        body,
    )


def test_scan_http_frames_responses_and_classifies_status() -> None:
    data = response(200, b'{"a":1}') + response(404, b"{}") + response(500, b"oops")
    assert [error for _, error in frames(scan_http, data)] == [False, False, True]


def test_scan_http_waits_for_headers_and_body() -> None:
    whole = response(200, b'{"key":"k","value":"v"}')
    for cut in range(len(whole)):
        assert scan_http(bytearray(whole[:cut]), 0) is None
    assert scan_http(bytearray(whole), 0) == (len(whole), False)


def test_scan_http_without_body() -> None:
    data = b"HTTP/1.1 204 No Content\r\nx-request-id: 1\r\n\r\n"
    assert scan_http(bytearray(data), 0) == (len(data), False)


def test_scan_http_rejects_chunked_and_non_http() -> None:
    chunked = b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n"
    with pytest.raises(FramingError, match="chunked"):
        scan_http(bytearray(chunked), 0)
    with pytest.raises(FramingError):
        scan_http(bytearray(b"+OK\r\n\r\n"), 0)


def test_http_requests() -> None:
    assert http_get(b"key:1").startswith(b"GET /v1/keys/key:1 HTTP/1.1\r\n")
    put = http_put(b"key:1", b"xyz")
    head, body = put.split(b"\r\n\r\n")
    assert head.startswith(b"PUT /v1/keys/key:1 HTTP/1.1\r\n")
    assert b"Content-Length: %d" % len(body) in head
    assert json.loads(body) == {"value": "xyz"}


def test_request_encoder_per_protocol() -> None:
    resp = RequestEncoder("resp", b"v")
    assert resp.get(b"k") == resp_command(b"GET", b"k")
    assert resp.set(b"k") == resp_command(b"SET", b"k", b"v")
    http = RequestEncoder("http", b"v")
    assert http.get(b"k") == http_get(b"k")
    assert http.set(b"k") == http_put(b"k", b"v")
