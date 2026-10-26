"""Task classes and per-server utilization (the follow-up "nodes" experiment)."""

import json

import pytest

from lbstudy import metrics, tasks
from lbstudy.experiment import RunConfig, run_once


def test_idle_while_others_wait_counts_wasted_capacity():
    # Server 0 is busy the whole 10 s window: one request until 8 s, then a
    # second one that waited from 0 s to 8 s. Server 1 sits idle, so for 8 of
    # its 10 s a request was waiting elsewhere: 80% of its time, 40% overall.
    busy = {"window_start": 100.0, "jobs": [0, 0, 8, 0, 8, 12], "resources": []}
    idle = {"window_start": 100.0, "jobs": [], "resources": []}
    row = metrics.node_summary([busy, idle], window_start=100.0, duration=10.0)
    assert row["busy_pct_per_server"] == "100.0|0.0"
    assert row["idle_while_waiting_pct"] == pytest.approx(40.0, abs=0.1)
    assert row["tasks_per_server"] == "1|0"


def test_task_classes_end_to_end(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(tasks.measure(samples=5)))
    workload = tasks.load(path)
    cfg = RunConfig(strategy="falb", staleness=0.5, load=0.5, warmup=1.0, duration=4.0,
                    n_servers=2, workload="classes", tasks_file=str(path),
                    mean_service=workload.mean_service)
    row = run_once(cfg, raw_dir=tmp_path)
    assert cfg.run_id.endswith("_N2_r0") and cfg.run_id.startswith("classes_")
    assert row["n_timeout"] == 0
    assert row["fast_n"] + row["moderate_n"] + row["slow_n"] == row["n_requests"]
    assert row["fast_n"] > row["slow_n"] > 0              # 60% vs 10% of requests
    assert row["fast_p99_ms"] < row["slow_p99_ms"]
    busy = list(map(float, row["busy_pct_per_server"].split("|")))
    assert len(busy) == 2 and all(20 < b < 85 for b in busy)    # ~50% load
    done = sum(map(int, row["tasks_per_server"].split("|")))
    assert done == pytest.approx(row["n_requests"], rel=0.05)
    assert (tmp_path / f"{cfg.run_id}.npz").exists()
