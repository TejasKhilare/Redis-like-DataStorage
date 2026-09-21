"""The suite's scenario matrix and summaries, and the report built from them."""

from pathlib import Path
from typing import Any

import pytest

from benchmarks.histogram import LatencyHistogram
from benchmarks.load_gen import LoadConfig, LoadResult
from benchmarks.pause import measure
from benchmarks.report import Charts, baseline, entity, markdown_tables
from benchmarks.suite import Scenario, scenarios, summarize


def fake_result(ops_per_sec: float, latency_us: int) -> LoadResult:
    hist = LatencyHistogram()
    hist.record(latency_us * 1000, count=int(ops_per_sec))
    return LoadResult(
        LoadConfig(duration_s=1.0), int(ops_per_sec), 0, 1.0, hist, [int(ops_per_sec)]
    )


def test_matrix_without_redis_has_no_redis_scenarios() -> None:
    matrix = scenarios(with_redis=False)
    assert {s.target for s in matrix} == {"kvstore", "router"}
    assert len(matrix) == len(set(matrix))


def test_matrix_covers_every_phase_3_comparison() -> None:
    matrix = scenarios(with_redis=True)
    groups = {s.group for s in matrix}
    assert groups == {"protocol", "fsync", "group-commit", "pipeline", "workload", "router"}
    for target in ("kvstore", "redis"):
        assert {s.fsync for s in matrix if s.target == target and s.group == "fsync"} == {
            "always", "everysec", "no",
        }  # fmt: skip
        assert {s.pipeline for s in matrix if s.target == target} == {1, 4, 16, 64}
    assert Scenario("protocol", "kvstore", protocol="http") in matrix
    assert Scenario("protocol", "kvstore").name == "kvstore/resp fsync=everysec get=90% P=1"


def test_summary_takes_the_median_and_merges_histograms() -> None:
    results = [fake_result(100, 1), fake_result(300, 3), fake_result(200, 2)]
    row = summarize(Scenario("protocol", "kvstore"), results)
    assert row["ops_per_sec"] == 200
    assert (row["ops_per_sec_min"], row["ops_per_sec_max"]) == (100, 300)
    assert row["reps"] == 3
    assert row["histogram"]["count"] == 600
    assert row["latency_us"]["max"] == 3.0  # microseconds, the slowest of all runs


def suite_data() -> dict[str, Any]:
    rows = []
    for scenario in scenarios(with_redis=True):
        speed = 20_000 if scenario.target == "redis" else 8_000
        speed *= scenario.pipeline**0.5
        rows.append(summarize(scenario, [fake_result(speed, 2), fake_result(speed * 1.1, 3)]))
    curve = [
        {
            "target": target,
            "fraction": fraction,
            "offered_ops_per_sec": int(10_000 * fraction),
            "achieved_ops_per_sec": 10_000 * fraction,
            "errors": 0,
            "latency_us": {p: 1000.0 * fraction for p in ("p50", "p99", "p99.9")},
        }
        for target in ("kvstore", "redis")
        for fraction in (0.5, 1.0)
    ]
    bench = [
        {"target": "redis", "pipeline": 1, "test": "SET", "ops_per_sec": 50_000.0,
         "p50_ms": 0.5, "p99_ms": 2.0, "max_ms": 9.0},
    ]  # fmt: skip
    return {
        "environment": {
            "date": "2026-09-21T00:00:00+00:00",
            "kvstore_version": "0.3.0",
            "git_commit": "abc1234",
            "platform": "Linux",
            "python": "3.12.3",
            "event_loop": "uvloop",
            "cpu": "Test CPU",
            "logical_cpus": 4,
            "memory_gb": 8.0,
            "load_average_at_start": ["0.1", "0.2", "0.3"],
            "redis_version": "Redis server v=7.2.7",
        },
        "options": {
            "clients": 50,
            "duration_s": 10,
            "warmup_s": 2,
            "reps": 2,
            "keyspace": 100_000,
            "value_size": 64,
        },
        "scenarios": rows,
        "latency_curve": curve,
        "redis_benchmark": bench,
    }


def test_rows_are_attributed_to_the_right_server() -> None:
    rows = suite_data()["scenarios"]
    http = baseline(rows, "http")
    assert http is not None and entity(http) == "http"
    router = baseline(rows, "router")
    assert router is not None and entity(router) == "router"
    assert baseline([], "kvstore") is None


def test_markdown_tables_include_every_run() -> None:
    data = suite_data()
    text = markdown_tables(data)
    assert "Test CPU (4 logical CPUs)" in text
    assert text.count("| fsync |") >= 1
    scenario_rows = [line for line in text.splitlines() if line.startswith("| protocol")]
    assert len(scenario_rows) == 3  # kvstore RESP, Redis, kvstore HTTP
    assert "| Redis 7.2 | SET | 1 | 50,000 |" in text
    assert "## Open loop" in text


def test_snapshot_pause_measurement_and_table() -> None:
    rows = [measure(500, "string", reps=2), measure(200, "hash", reps=1)]
    assert rows[0]["pause_ms"] >= 0
    assert rows[0]["pause_ms_max"] >= rows[0]["pause_ms"]
    assert rows[0]["snapshot_bytes"] > 500 * 64  # every value made it into the snapshot
    data = {**suite_data(), "snapshot_pause": rows}
    text = markdown_tables(data)
    assert "## BGREWRITEAOF" in text
    assert "| hash | 200 |" in text


def test_charts_render_in_both_themes(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    names = Charts(suite_data(), tmp_path).all()
    charts = {"throughput", "latency-percentiles", "pipelining", "fsync", "workload",
              "latency-vs-load"}  # fmt: skip
    assert set(names) == {f"{c}-{t}.png" for c in charts for t in ("light", "dark")}
    assert all((tmp_path / name).stat().st_size > 10_000 for name in names)
