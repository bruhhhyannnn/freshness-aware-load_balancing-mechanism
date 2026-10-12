"""End-to-end: start real server and balancer processes and push traffic through."""

import pytest

from lbstudy.experiment import RunConfig, run_once


def check_run(row, cfg, raw_dir):
    assert row["n_timeout"] == 0
    assert row["n_ok"] == row["n_requests"]
    # Every request was dispatched to some server, and all five got traffic.
    dispatched = list(map(int, row["dispatched_per_server"].split("|")))
    assert len(dispatched) == 5 and min(dispatched) > 0
    assert sum(dispatched) == pytest.approx(row["n_requests"], abs=5)
    assert (raw_dir / f"{cfg.run_id}.npz").exists()


@pytest.mark.parametrize("strategy,staleness", [
    ("round_robin", 0.0),
    ("least_loaded", 0.0),      # fresh probes of all five servers per request
    ("p2c", 0.0),               # fresh probes of the two sampled servers
    ("p2c", 0.5),               # periodic snapshot
    ("falb", 0.0),
    ("falb", 0.5),
])
def test_short_run(strategy, staleness, tmp_path):
    cfg = RunConfig(strategy=strategy, staleness=staleness, load=0.5, warmup=1.0, duration=4.0)
    row = run_once(cfg, raw_dir=tmp_path)
    check_run(row, cfg, tmp_path)
    # 125 req/s for 4 s: a Poisson count, comfortably within +-25 %.
    assert 375 < row["n_requests"] < 625
    # Service alone averages 20 ms; at 50 % load queueing roughly doubles it.
    assert 15 < row["mean_ms"] < 120
    assert row["decision_us"] > 0
    if strategy == "round_robin":
        assert row["dispatch_cov"] < 0.01
    if strategy == "falb":
        expected = "mode_all" if staleness == 0 else "mode_two"
        assert row[expected] > 0.5


def test_missing_report_scenario_mutes_the_server(tmp_path, monkeypatch):
    # Shrink the scenario so the test stays short: event 2 s into the window.
    from lbstudy import config
    monkeypatch.setattr(config, "EVENT_START_S", 2.0)
    monkeypatch.setitem(config.SCENARIOS, "missing_report",
                        dict(load=0.5, staleness=0.25, event="mute", duration=3.0))
    cfg = RunConfig.for_scenario("falb", "missing_report", warmup=1.0, duration=7.0)
    row = run_once(cfg, raw_dir=tmp_path)
    check_run(row, cfg, tmp_path)
    assert row["lb_missed_probes"] >= 8          # ~12 polls fall inside the 3 s silence
    assert row["mode_random"] > 0                # FALB timed the silent server out
    assert row["event_p99_ms"] is not None


def test_several_balancers(tmp_path, monkeypatch):
    from lbstudy import config
    monkeypatch.setitem(config.SCENARIOS, "shared_snapshot",
                        dict(load=0.5, staleness=0.5, balancers=3))
    cfg = RunConfig.for_scenario("falb", "shared_snapshot", warmup=1.0, duration=4.0)
    row = run_once(cfg, raw_dir=tmp_path)
    check_run(row, cfg, tmp_path)
    assert row["balancers"] == 3
