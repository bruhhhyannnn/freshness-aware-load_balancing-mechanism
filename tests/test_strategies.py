import random
from collections import Counter

import pytest

from lbstudy.strategies import (FreshnessAware, LeastLoaded, PowerOfD, RandomChoice, Report,
                                RoundRobin, make_strategy)


def feed(strategy, queues, now=0.0, completed=None, busy=None):
    """Give the strategy one report per server, all produced at ``now``."""
    for i, q in enumerate(queues):
        strategy.on_report(i, Report(q, now, completed[i] if completed else 0,
                                     busy[i] if busy else 0.0), now)


def route(strategy, now=0.0):
    target = strategy.pick(strategy.candidates(), now)
    strategy.on_dispatch(target, now)
    return target


# --- baselines -------------------------------------------------------------------

def test_round_robin_cycles_through_all_servers():
    rr = RoundRobin(5)
    assert [route(rr) for _ in range(12)] == [0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1]


def test_random_is_roughly_uniform():
    rnd = RandomChoice(5, random.Random(1))
    counts = Counter(route(rnd) for _ in range(50_000))
    assert set(counts) == set(range(5))
    assert max(counts.values()) / min(counts.values()) < 1.1


def test_least_loaded_picks_the_minimum():
    ll = LeastLoaded(5, random.Random(1))
    feed(ll, [4, 2, 7, 1, 3])
    assert route(ll) == 3


def test_least_loaded_breaks_ties_at_random():
    ll = LeastLoaded(5, random.Random(1))
    feed(ll, [0, 5, 0, 5, 0])
    assert set(Counter(route(ll) for _ in range(3000))) == {0, 2, 4}


def test_least_loaded_herds_on_a_stale_snapshot():
    """With no new report, every request goes to the server that looked idle."""
    ll = LeastLoaded(5, random.Random(1))
    feed(ll, [3, 3, 0, 3, 3])
    assert {route(ll, now=t / 100) for t in range(200)} == {2}


def test_p2c_samples_two_distinct_servers_uniformly():
    p2c = PowerOfD(5, random.Random(1), d=2)
    pairs = Counter()
    for _ in range(50_000):
        a, b = p2c.candidates()
        assert a != b
        pairs[frozenset((a, b))] += 1
    assert len(pairs) == 10                       # all C(5, 2) pairs occur
    assert max(pairs.values()) / min(pairs.values()) < 1.15


def test_p2c_never_picks_the_most_loaded_server():
    p2c = PowerOfD(5, random.Random(1), d=2)
    feed(p2c, [0, 0, 0, 0, 99])
    assert all(route(p2c) != 4 for _ in range(2000))


# --- FALB -----------------------------------------------------------------------

def warmed_falb(**kw) -> FreshnessAware:
    """FALB that has seen two reports, so it knows each server's processing rate (50/s)."""
    falb = FreshnessAware(5, random.Random(1), **kw)
    feed(falb, [0] * 5, now=0.0)
    feed(falb, [0] * 5, now=1.0, completed=[50] * 5, busy=[1.0] * 5)
    return falb


def test_falb_measures_the_processing_rate():
    falb = warmed_falb()
    assert falb.rate(0) == pytest.approx(50.0)


def test_falb_correction_defeats_herding():
    """Sent requests raise the estimate at once, so the 'idle' server stops looking idle."""
    falb = warmed_falb()
    feed(falb, [3, 3, 0, 3, 3], now=1.0, completed=[50] * 5, busy=[1.0] * 5)
    picks = Counter(route(falb, now=1.0) for _ in range(20))
    # Water-filling: 3 requests bring server 2 level with the others, then the
    # remaining 17 are shared evenly. Least-Loaded would send all 20 to it.
    assert picks[2] <= 3 + 4
    assert min(picks[i] for i in range(5)) >= 3


def test_falb_estimate_drains_at_the_measured_rate_but_not_below_zero():
    falb = warmed_falb()
    feed(falb, [10] * 5, now=1.0, completed=[50] * 5, busy=[1.0] * 5)
    assert falb.estimate(0, 1.1) == pytest.approx(10 - 50 * 0.1)
    assert falb.estimate(0, 5.0) == 0.0


def test_falb_modes_follow_the_age_of_information():
    falb = warmed_falb(a1=0.1, a2=2.0)
    route(falb, now=1.05)                       # 0.05 s old: compare all
    route(falb, now=1.5)                        # 0.5 s old: compare two
    route(falb, now=3.5)                        # 2.5 s old: timed out, random
    assert falb.modes == {"all": 1, "two": 1, "random": 1}


def test_falb_gives_a_silent_server_only_its_random_share():
    falb = warmed_falb(a1=0.1, a2=2.0)
    for t in (2.0, 3.0, 4.0):                   # server 4 stops reporting after t = 1
        for i in range(4):
            falb.on_report(i, Report(0, t, 50 * int(t), t), t)
    counts = Counter(route(falb, now=4.0 + k * 1e-4) for k in range(5000))
    # Server 4 (silent, last reported empty) gets ~1/5 at random, not everything.
    assert 0.15 < counts[4] / 5000 < 0.25


def test_falb_variants():
    assert make_strategy("falb", 5).correct and make_strategy("falb", 5).adaptive
    assert not make_strategy("falb_nocorr", 5).correct
    assert not make_strategy("falb_noadapt", 5).adaptive
    assert make_strategy("falb_cpu", 5).signal == "cpu"
    assert make_strategy("falb_turns", 5).fallback == "turns"
    assert make_strategy("falb", 5, a1=0.2, a2=3.0).a2 == 3.0


def test_make_strategy_names():
    assert make_strategy("rr", 3).name == "round_robin"
    assert make_strategy("least_loaded", 3).name == "least_loaded"
    assert make_strategy("p3c", 5).d == 3
    with pytest.raises(ValueError):
        make_strategy("p6c", 5)
    with pytest.raises(ValueError):
        make_strategy("fastest", 5)


def test_falb_turn_taking_fallback_cycles_through_timed_out_servers():
    falb = warmed_falb(fallback="turns")
    picks = [route(falb, now=10.0) for _ in range(10)]   # every report is 9 s old
    assert sorted(picks) == sorted(list(range(5)) * 2)
