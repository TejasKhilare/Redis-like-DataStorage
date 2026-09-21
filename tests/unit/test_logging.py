import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from kvstore.core.logging import JsonFormatter, configure_logging
from tests.helpers import make_settings


@pytest.fixture
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def make_record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord("kvstore.test", logging.INFO, __file__, 1, "hello %s", ("x",), None)
    record.__dict__.update(extra)
    return record


def test_json_formatter_includes_extras() -> None:
    line = JsonFormatter(node_id="shard-1").format(make_record(keys=3, _private=1))
    payload = json.loads(line)

    assert payload["msg"] == "hello x"
    assert payload["level"] == "INFO"
    assert payload["node"] == "shard-1"
    assert payload["keys"] == 3
    assert "_private" not in payload
    assert payload["ts"].endswith("+00:00")


def test_json_formatter_includes_exceptions() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = make_record()
        record.exc_info = sys.exc_info()
    assert "ValueError: boom" in json.loads(JsonFormatter().format(record))["exc_info"]


@pytest.mark.usefixtures("restore_root_logger")
@pytest.mark.parametrize("log_format", ["json", "text"])
def test_configure_logging(tmp_path: Path, log_format: str) -> None:
    configure_logging(make_settings(tmp_path, log_format=log_format, log_level="warning"))

    root = logging.getLogger()
    assert root.level == logging.WARNING
    formatter = root.handlers[0].formatter
    assert isinstance(formatter, JsonFormatter) == (log_format == "json")
