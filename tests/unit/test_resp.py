import pytest

from kvstore.core.exceptions import (
    CommandError,
    CrossShardError,
    KVStoreError,
    NodeUnavailableError,
    OutOfMemoryError,
    PersistenceWriteError,
    ProtocolError,
    WrongTypeError,
)
from kvstore.protocol.resp import (
    NOT_READY,
    OK,
    ReplyParser,
    RequestParser,
    SimpleString,
    encode_command,
    encode_error,
    encode_reply,
    format_float,
)


def parse_all(data: bytes, parser: RequestParser | None = None) -> list[list[str]]:
    parser = parser or RequestParser()
    parser.feed(data)
    commands = []
    while (command := parser.next_command()) is not None:
        commands.append(command)
    return commands


# ------------------------------------------------------------- requests
def test_multibulk_request() -> None:
    assert parse_all(b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$5\r\nhello\r\n") == [["SET", "k", "hello"]]


def test_pipelined_requests() -> None:
    data = encode_command(["SET", "a", "1"]) + encode_command(["GET", "a"]) + b"PING\r\n"
    assert parse_all(data) == [["SET", "a", "1"], ["GET", "a"], ["PING"]]


def test_request_split_at_every_byte() -> None:
    data = encode_command(["SET", "key", "a value with spaces\r\nand CRLF"]) + b"PING\r\n"
    parser = RequestParser()
    commands = []
    for i in range(len(data)):
        parser.feed(data[i : i + 1])
        while (command := parser.next_command()) is not None:
            commands.append(command)
    assert commands == [["SET", "key", "a value with spaces\r\nand CRLF"], ["PING"]]


def test_inline_commands_and_empty_lines() -> None:
    assert parse_all(b"PING\r\n\r\nECHO   hi  \nGET k\r\n") == [
        ["PING"],
        [],
        ["ECHO", "hi"],
        ["GET", "k"],
    ]


def test_empty_multibulk_is_skipped() -> None:
    assert parse_all(b"*0\r\n*-1\r\n") == [[], []]


def test_binary_values_round_trip() -> None:
    raw = bytes(range(256))
    (command,) = parse_all(encode_command([b"SET", b"k", raw]))
    assert encode_reply(command[2]) == b"$256\r\n" + raw + b"\r\n"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"*x\r\n", "invalid multibulk length"),
        (b"*1\r\n:5\r\n", r"expected '\$', got ':'"),
        (b"*1\r\n$x\r\n", "invalid bulk length"),
        (b"*1\r\n$-5\r\n", "invalid bulk length"),
        (b"*1\r\n$3\r\nabcXY", "invalid bulk string terminator"),
        (b"*99999999\r\n", "invalid multibulk length"),
    ],
)
def test_malformed_requests(data: bytes, message: str) -> None:
    with pytest.raises(ProtocolError, match=message):
        parse_all(data)


def test_limits() -> None:
    with pytest.raises(ProtocolError, match="invalid bulk length"):
        parse_all(b"*1\r\n$11\r\n", RequestParser(max_bulk=10))
    with pytest.raises(ProtocolError, match="too big"):
        parse_all(b"x" * 70_000)


# --------------------------------------------------------------- replies
@pytest.mark.parametrize(
    ("value", "wire"),
    [
        (OK, b"+OK\r\n"),
        ("hi", b"$2\r\nhi\r\n"),
        ("", b"$0\r\n\r\n"),
        (None, b"$-1\r\n"),
        (42, b":42\r\n"),
        (True, b":1\r\n"),
        (1.5, b"$3\r\n1.5\r\n"),
        (["a", 1, None, ["b"]], b"*4\r\n$1\r\na\r\n:1\r\n$-1\r\n*1\r\n$1\r\nb\r\n"),
        ((), b"*0\r\n"),
    ],
)
def test_encode_reply(value: object, wire: bytes) -> None:
    assert encode_reply(value) == wire


def test_encode_rejects_unknown_types() -> None:
    with pytest.raises(TypeError):
        encode_reply(object())


def test_errors_encode_with_prefix_and_no_newlines() -> None:
    assert encode_error(WrongTypeError()) == (
        b"-WRONGTYPE Operation against a key holding the wrong kind of value\r\n"
    )
    assert encode_error(CommandError("bad\r\nthing")) == b"-ERR bad  thing\r\n"
    assert encode_reply(CommandError("x")) == b"-ERR x\r\n"


def test_reply_parser_round_trip_and_partial_input() -> None:
    values = [OK, "bulk", None, -7, ["nested", ["deep", None]], []]
    wire = b"".join(encode_reply(v) for v in values)
    parser = ReplyParser()
    got = []
    for i in range(len(wire)):
        parser.feed(wire[i : i + 1])
        while (reply := parser.next_reply()) is not NOT_READY:
            got.append(reply)
    assert got == values
    assert isinstance(got[0], SimpleString)


def test_reply_parser_null_array_and_errors() -> None:
    parser = ReplyParser()
    parser.feed(b"*-1\r\n-WRONGTYPE nope\r\n-ERR plain\r\n")
    assert parser.next_reply() is None
    error = parser.next_reply()
    assert isinstance(error, WrongTypeError)
    assert error.message == "nope"
    assert isinstance(parser.next_reply(), CommandError)
    assert parser.next_reply() is NOT_READY


def test_reply_parser_rejects_garbage() -> None:
    parser = ReplyParser()
    parser.feed(b"?what\r\n")
    with pytest.raises(ProtocolError):
        parser.next_reply()


# ---------------------------------------------------------------- errors
@pytest.mark.parametrize(
    ("line", "cls"),
    [
        ("WRONGTYPE Operation against a key", WrongTypeError),
        ("CROSSSLOT Keys in request", CrossShardError),
        ("CLUSTERDOWN node down", NodeUnavailableError),
        ("OOM command not allowed", OutOfMemoryError),
        ("MISCONF Errors writing", PersistenceWriteError),
        ("ERR syntax error", CommandError),
    ],
)
def test_error_prefixes_map_to_types(line: str, cls: type[KVStoreError]) -> None:
    error = KVStoreError.from_resp(line)
    assert type(error) is cls
    assert error.to_resp() == line


def test_unknown_prefix_is_preserved() -> None:
    error = KVStoreError.from_resp("NOPROTO sorry")
    assert isinstance(error, CommandError)
    assert error.to_resp() == "NOPROTO sorry"
    assert KVStoreError.from_resp("lowercase message").to_resp() == "ERR lowercase message"


def test_format_float() -> None:
    assert format_float(3.0) == "3"
    assert format_float(-0.5) == "-0.5"
    assert format_float(float("inf")) == "inf"
    assert format_float(float("-inf")) == "-inf"
    assert format_float(1e20) == "1e+20"
    assert float(format_float(0.1 + 0.2)) == 0.1 + 0.2
