"""Metric primitives and the Prometheus text format."""

import math
from typing import Any

from prometheus_client.parser import text_string_to_metric_families

from kvstore.observability.metrics import CommandStats, Exposition, Histogram, process_metrics


def families(text: str) -> dict[str, Any]:
    """Parse with the official parser: it rejects anything malformed."""
    return {family.name: family for family in text_string_to_metric_families(text)}


def test_histogram_buckets_are_cumulative() -> None:
    hist = Histogram((0.1, 1.0))
    for value in (0.05, 0.1, 0.5, 2.0, 3.0):
        hist.observe(value)
    assert hist.cumulative() == [("0.1", 2), ("1", 3), ("+Inf", 5)]
    assert hist.count == 5
    assert math.isclose(hist.sum, 5.65)
    locked = Histogram((1,), locked=True)
    locked.observe(0.5)
    assert locked.cumulative() == [("1", 1), ("+Inf", 1)]


def test_command_stats() -> None:
    stats = CommandStats()
    stats.record("get", 0.001)
    stats.record("get", 0.002)
    stats.error("get", "WRONGTYPE")
    assert stats.calls == {"get": 2}
    assert stats.errors == {("get", "WRONGTYPE"): 1}
    assert stats.latency["get"].count == 2


def test_exposition_parses_with_the_official_parser() -> None:
    out = Exposition()
    out.counter("kv_requests", "Requests.", [({"command": "get"}, 3), ({"command": "set"}, 1)])
    out.gauge("kv_keys", "Keys.", [({}, 42)])
    out.gauge("kv_empty", "Nothing to report.", [])  # omitted entirely
    hist = Histogram((0.01, 0.1))
    hist.observe(0.05)
    out.histogram("kv_latency_seconds", "Latency.", [({"command": "get"}, hist)])
    out.gauge("kv_odd", "Label escaping.", [({"v": 'a "quoted" \\ back\nslash'}, 1.5)])
    text = out.text()
    assert "# TYPE kv_requests_total counter" in text
    assert "kv_empty" not in text

    parsed = families(text)
    requests = parsed["kv_requests"]
    assert {s.labels["command"]: s.value for s in requests.samples} == {"get": 3, "set": 1}
    latency = parsed["kv_latency_seconds"]
    buckets = {s.labels["le"]: s.value for s in latency.samples if s.name.endswith("_bucket")}
    assert buckets == {"0.01": 0, "0.1": 1, "+Inf": 1}
    (odd,) = parsed["kv_odd"].samples
    assert odd.labels["v"] == 'a "quoted" \\ back\nslash'
    assert odd.value == 1.5


def test_process_metrics() -> None:
    out = Exposition()
    process_metrics(out)
    parsed = families(out.text())
    assert "process_cpu_seconds" in parsed
    assert "process_start_time_seconds" in parsed
