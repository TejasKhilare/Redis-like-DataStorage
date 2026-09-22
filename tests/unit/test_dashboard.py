"""The Grafana dashboard and Prometheus config agree with the code and docker-compose.yml.

A renamed metric would leave a dashboard panel silently empty; these tests
catch that without running Prometheus or Grafana. (That every query returns
data from a live cluster was checked once by hand -- see docs/BENCHMARKS.md.)
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
DASHBOARD = DEPLOY / "grafana" / "dashboards" / "kvstore.json"


def emitted_metric_names() -> set[str]:
    """Every series name the collectors can produce, read from their source."""
    names: set[str] = set()
    for path in (ROOT / "src" / "kvstore" / "observability").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for kind, name in re.findall(r"\.(counter|gauge|histogram)\(\s*\"(\w+)\"", source):
            if kind == "counter":
                names.add(name + "_total")
            elif kind == "histogram":
                names.update(name + suffix for suffix in ("_bucket", "_sum", "_count"))
            else:
                names.add(name)
    return names


def panels() -> list[dict[str, Any]]:
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    return [p for p in dashboard["panels"] if p["type"] != "row"]


def exprs() -> list[str]:
    return [target["expr"] for panel in panels() for target in panel["targets"]]


def test_the_collectors_are_found() -> None:
    names = emitted_metric_names()
    assert {"kvstore_commands_total", "kvstore_keys", "process_cpu_seconds_total"} <= names
    assert "kvstore_command_duration_seconds_bucket" in names


@pytest.mark.parametrize("expr", exprs())
def test_every_query_uses_metrics_the_nodes_expose(expr: str) -> None:
    used = set(re.findall(r"\b(?:kvstore|process)_[a-z_]+\b", expr))
    assert used, expr
    assert used <= emitted_metric_names(), used - emitted_metric_names()


def test_panels_are_well_formed() -> None:
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    ids = [p["id"] for p in dashboard["panels"]]
    assert len(ids) == len(set(ids))
    datasource = re.search(
        r"uid:\s*(\S+)",
        (DEPLOY / "grafana" / "provisioning" / "datasources" / "prometheus.yml").read_text(
            encoding="utf-8"
        ),
    )
    assert datasource is not None
    for panel in panels():
        assert panel["targets"], panel["title"]
        assert panel["datasource"]["uid"] == datasource.group(1)
        assert [t["refId"] for t in panel["targets"]] == [
            chr(ord("A") + i) for i in range(len(panel["targets"]))
        ]


def test_every_compose_node_is_scraped() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    nodes = set(re.findall(r"^  ([\w-]+):\n    <<: \*node$", compose, flags=re.M))
    assert {"router", "shard-1", "shard-1-replica"} <= nodes
    prometheus = (DEPLOY / "prometheus" / "prometheus.yml").read_text(encoding="utf-8")
    targets = set(re.findall(r"([\w-]+):8000", prometheus))
    assert targets == nodes
    jobs = set(re.findall(r"job_name:\s*(\S+)", prometheus))
    for expr in exprs():
        assert set(re.findall(r'job="([^"]+)"', expr)) <= jobs, expr
