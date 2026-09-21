"""The suite end to end: a real kvstore process, preloaded and measured, then torn down."""

import json
import shutil
from pathlib import Path

from benchmarks.report import main as report_main
from benchmarks.servers import Deployment
from benchmarks.suite import Scenario, SuiteOptions, main, run_scenario
from benchmarks.workload import Workload

QUICK = SuiteOptions(clients=4, duration_s=0.5, warmup_s=0.1, workload=Workload(keyspace=100))


def test_scenario_against_a_real_node() -> None:
    result = run_scenario(Scenario("fsync", "kvstore", fsync="always", read_ratio=0.0),
                          QUICK)  # fmt: skip
    assert result.ops > 0
    assert result.errors == 0


def test_deployment_cleans_up_after_itself(tmp_path: Path) -> None:
    with Deployment(tmp_path) as deployment:
        endpoint = deployment.kvstore_node("node", fsync="no")
        assert endpoint.http_port is not None
        work = deployment.dir
        assert (work / "node").is_dir()  # the node's data directory
    assert not work.exists()


def test_suite_cli_and_report(tmp_path: Path) -> None:
    out = tmp_path / "run.json"
    code = main([
        "--out", str(out), "--groups", "protocol", "--reps", "1",
        "--duration", "0.5", "--warmup", "0.1", "--clients", "4", "--keys", "100",
        "--no-curve", "--work-dir", str(tmp_path),
    ])  # fmt: skip
    assert code == 0
    data = json.loads(out.read_text())
    names = {row["scenario"] for row in data["scenarios"]}
    assert names == {"kvstore/resp fsync=everysec get=90% P=1",
                     "kvstore/http fsync=everysec get=90% P=1"}  # fmt: skip
    assert all(row["errors"] == 0 and row["ops_per_sec"] > 0 for row in data["scenarios"])
    assert data["environment"]["kvstore_version"]

    docs = tmp_path / "docs"
    assert report_main([str(out), "--out-dir", str(docs), "--no-charts"]) == 0
    assert "kvstore (HTTP)" in (docs / "results.md").read_text()
    shutil.rmtree(docs)
