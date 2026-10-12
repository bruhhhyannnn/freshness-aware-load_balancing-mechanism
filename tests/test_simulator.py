"""The simulator must reproduce known queueing results before it is trusted."""

import pytest

from lbstudy import metrics
from lbstudy.simulate import simulate


@pytest.mark.parametrize("load", [0.5, 0.8])
def test_random_matches_mm1_theory(load):
    # Random splitting of Poisson traffic gives n independent M/M/1 queues.
    row = simulate("random", 5, load, 0.0, duration=1500, warmup=100)
    assert row["mean_ms"] == pytest.approx(metrics.mm1_mean_ms(load, 0.02), rel=0.05)
    assert row["p99_ms"] == pytest.approx(metrics.mm1_percentile_ms(load, 0.02, 99), rel=0.08)


def test_single_server_is_mm1():
    row = simulate("round_robin", 1, 0.7, 0.0, duration=2000, warmup=100)
    assert row["mean_ms"] == pytest.approx(metrics.mm1_mean_ms(0.7, 0.02), rel=0.05)


def test_fresh_information_ordering():
    """With fresh information: Least-Loaded <= P2C < Round Robin < Random."""
    p99 = {s: simulate(s, 5, 0.9, 0.0, duration=600)["p99_ms"]
           for s in ("least_loaded", "p2c", "round_robin", "random")}
    assert p99["least_loaded"] <= p99["p2c"] < p99["round_robin"] < p99["random"]


def test_falb_equals_least_loaded_with_fresh_information():
    a = simulate("falb", 5, 0.8, 0.0, duration=200, warmup=20)
    b = simulate("least_loaded", 5, 0.8, 0.0, duration=200, warmup=20)
    assert a["p99_ms"] == pytest.approx(b["p99_ms"], rel=0.1)
    assert a["mode_all"] == 1.0


def test_staleness_does_not_affect_load_blind_strategies():
    for s in ("round_robin", "random"):
        a = simulate(s, 5, 0.8, 0.0, duration=200, warmup=20)
        b = simulate(s, 5, 0.8, 2.0, duration=200, warmup=20)
        assert a["p99_ms"] == b["p99_ms"]


def test_slow_server_scenario_overloads_round_robin():
    rr = simulate("round_robin", 5, 0.8, 0.5, scenario="slow_server", duration=60, warmup=10)
    calm = simulate("round_robin", 5, 0.8, 0.5, duration=60, warmup=10)
    assert rr["event_p99_ms"] > 5 * calm["p99_ms"]


def test_every_request_is_assigned():
    row = simulate("p2c", 5, 0.5, 0.5, duration=100, warmup=10, return_requests=True)
    lat = row["requests"]["latency"]
    assert row["n_requests"] == len(lat) > 10_000
    assert (lat > 0).all()
