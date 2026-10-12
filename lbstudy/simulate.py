"""Discrete-event simulation of the testbed.

The simulator models exactly what the testbed runs: Poisson arrivals, n FIFO
servers with exponential processing times, status reports (queue length,
timestamp, completed count, busy time) polled every T seconds, the same
event scenarios, and the very same strategy classes. It runs in virtual time,
so hours of traffic take seconds. It serves three purposes:

* pilot:       tune FALB's thresholds before spending hours on the testbed
* validation:  at n = 5 its curves should match the testbed's measurements
* extension:   larger server pools, the CPU-signal variant, and an ablation
               study of FALB's components

Because each server is FIFO and a job's processing time is drawn when it
arrives, its departure time is known immediately: max(arrival, previous
departure) + processing time. Each server is therefore a deque of departure
times, and its queue length at time t is the number of entries later than t.

Examples:
    python -m lbstudy.simulate one --strategy falb --staleness 1 --load 0.9
    python -m lbstudy.simulate one --strategy falb --scenario slow_server
    python -m lbstudy.simulate validate     # n = 5, the testbed's grid
    python -m lbstudy.simulate extend       # pools of 5/20/100, variants
"""

from __future__ import annotations

import argparse
import csv
import itertools
import math
import os
import random
import time
from array import array
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from . import config, metrics
from .strategies import Report, label, make_strategy

EXTENDED_STALENESS = (0.0, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0)
ABLATION_STRATEGIES = ("p2c", "falb", "falb_nocorr", "falb_noadapt", "falb_cpu", "falb_turns")
LARGE_POOLS = (20, 100)
BALANCER_COUNTS = (1, 2, 4)


class _Server:
    """One simulated FIFO server."""

    __slots__ = ("departures", "services", "last", "completed", "busy_done", "rng", "mu")

    def __init__(self, rng: random.Random, mean_service: float):
        self.departures: deque[float] = deque()
        self.services: deque[float] = deque()
        self.last = 0.0
        self.completed = 0
        self.busy_done = 0.0
        self.rng = rng
        self.mu = 1.0 / mean_service

    def advance(self, t: float) -> None:
        deps, svcs = self.departures, self.services
        while deps and deps[0] <= t:
            deps.popleft()
            self.busy_done += svcs.popleft()
            self.completed += 1

    def report(self, t: float) -> Report:
        self.advance(t)
        busy = self.busy_done
        if self.departures:                       # include the job in service
            started = self.departures[0] - self.services[0]
            if t > started:
                busy += t - started
        return Report(len(self.departures), t, self.completed, busy)

    def admit(self, t: float, slow_factor) -> tuple[float, int]:
        self.advance(t)
        start = t if t > self.last else self.last
        service = self.rng.expovariate(self.mu) * slow_factor(start)
        self.last = start + service
        self.departures.append(self.last)
        self.services.append(service)
        return self.last, len(self.departures)


def simulate(strategy: str, n_servers: int, load: float, staleness: float, *,
             scenario: str = "staleness", mean_service: float = config.MEAN_SERVICE_S,
             duration: float = 600.0, warmup: float = 60.0, rep: int = 0, balancers: int = 1,
             shared_snapshot: bool = False, falb_params: dict | None = None, return_requests: bool = False) -> dict:
    """Simulate one configuration; returns the same metrics as a testbed run.

    ``balancers`` > 1 runs several independent load balancers side by side.
    Each request goes to one of them at random, each polls the servers on
    its own schedule, and each knows only about the requests it sent itself.
    With ``shared_snapshot`` they all receive the same reports at the same
    moments instead (a central health checker / service registry).
    """
    # Seeds depend on (n, load, rep) only: common random numbers across
    # strategies, staleness levels and scenarios, as in the testbed.
    base = f"{n_servers}-{load}-{rep}"
    arrivals = random.Random(f"arrivals-{base}")
    servers = [_Server(random.Random(f"service-{base}-{i}"), mean_service)
               for i in range(n_servers)]
    params = (falb_params or {}) if strategy.startswith("falb") else {}
    lbs = [make_strategy(strategy, n_servers, random.Random(f"strategy-{strategy}-{base}-{b}"),
                         **params) for b in range(balancers)]
    lb_choice = random.Random(f"balancer-{base}")

    sc = config.SCENARIOS.get(scenario, {})
    event = sc.get("event")
    ev_server = config.EVENT_SERVER if event else -1
    ev_start = warmup + config.EVENT_START_S
    ev_end = ev_start + sc.get("duration", 0.0)
    slow = event == "slow"
    factor = sc.get("factor", 1.0)
    normal = lambda s: 1.0                                          # noqa: E731
    slowed = lambda s: factor if ev_start <= s < ev_end else 1.0     # noqa: E731

    def silent(i: int, t: float) -> bool:
        return event == "mute" and i == ev_server and ev_start <= t < ev_end

    rate = load * n_servers / mean_service
    periodic = lbs[0].uses_load and staleness > 0
    fresh = lbs[0].uses_load and staleness == 0
    # Each balancer polls every T seconds, at its own phase.
    next_poll = [arrivals.uniform(0, staleness) if periodic else math.inf for _ in lbs]
    if shared_snapshot:
        next_poll = [next_poll[0]] * balancers
    end = warmup + duration

    arrival_times, latencies, targets = array("d"), array("d"), array("h")
    counts = [0] * n_servers
    max_queue = [0] * n_servers
    window_open = False

    t = 0.0
    while True:
        t += arrivals.expovariate(rate)
        if t >= end:
            break
        if not window_open and t >= warmup:
            window_open = True
            for lb in lbs:
                lb.reset_stats()
        while True:                                        # periodic status polls
            b = min(range(balancers), key=next_poll.__getitem__)
            tp = next_poll[b]
            if tp > t:
                break
            for i in range(n_servers):
                if not silent(i, tp):
                    lbs[b].on_report(i, servers[i].report(tp), tp)
            next_poll[b] = tp + staleness

        strat = lbs[lb_choice.randrange(balancers)] if balancers > 1 else lbs[0]
        candidates = strat.candidates()
        if fresh:                                          # T = 0: probe now
            for i in candidates:
                if not silent(i, t):
                    strat.on_report(i, servers[i].report(t), t)
        target = strat.pick(candidates, t)
        strat.on_dispatch(target, t)
        done, held = servers[target].admit(t, slowed if slow and target == ev_server else normal)
        if window_open:
            arrival_times.append(t - warmup)
            latencies.append(done - t)
            targets.append(target)
            counts[target] += 1
            if held > max_queue[target]:
                max_queue[target] = held

    lat = np.frombuffer(latencies, dtype=float)
    sched = np.frombuffer(arrival_times, dtype=float)
    row = {
        "strategy": strategy, "scenario": scenario, "staleness": staleness, "load": load,
        "rep": rep, "n_servers": n_servers, "balancers": balancers,
        "shared_snapshot": shared_snapshot,
        "mean_service_ms": 1000 * mean_service,
        "warmup_s": warmup, "duration_s": duration,
        **metrics.latency_summary(lat, math.inf),
        "max_queue": max(max_queue),
        "dispatch_cov": metrics.coefficient_of_variation(counts),
        **_mean_stats(lb.stats() for lb in lbs),
    }
    if event:
        during = (sched >= ev_start - warmup) & (sched < ev_end - warmup)
        ev = metrics.latency_summary(lat[during], math.inf)
        row.update(event_mean_ms=ev["mean_ms"], event_p99_ms=ev["p99_ms"])
    if return_requests:
        row["requests"] = {"scheduled": sched.copy(),
                           "latency": lat.copy(),
                           "server": np.frombuffer(targets, dtype=np.int16).copy()}
    return row


def _mean_stats(stats) -> dict:
    stats = list(stats)
    return {k: float(np.mean([s[k] for s in stats])) for k in stats[0]} if stats[0] else {}


def _simulate_kwargs(kwargs: dict) -> dict:
    return simulate(**kwargs)


def run_jobs(out_csv: Path, jobs: list[dict], workers: int | None = None) -> None:
    # Big pools with fresh probing are the slowest jobs: start them first.
    jobs = sorted(jobs, key=lambda j: -j["n_servers"] * (1 + 4 * (j["staleness"] == 0)))
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    rows = []
    with ProcessPoolExecutor(max_workers=workers or max(1, (os.cpu_count() or 2) - 1)) as pool:
        for k, row in enumerate(pool.map(_simulate_kwargs, jobs, chunksize=1), 1):
            rows.append(row)
            if k % 100 == 0 or k == len(jobs):
                print(f"  {k}/{len(jobs)} simulations ({time.time() - started:.0f} s)", flush=True)
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with open(out_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {out_csv}")


def scenario_kwargs(name: str) -> dict:
    sc = config.SCENARIOS.get(name, {})
    return {"balancers": sc["balancers"], "shared_snapshot": True} if "balancers" in sc else {}


def grid_jobs(servers, strategies, staleness, loads, reps, duration, warmup) -> list[dict]:
    jobs = [dict(strategy=s, n_servers=n, load=l, staleness=T, rep=r,
                 duration=duration, warmup=warmup)
            for n, s, l, T, r in itertools.product(servers, strategies, loads, staleness,
                                                   range(reps))]
    for name, sc in config.SCENARIOS.items():
        jobs += [dict(strategy=s, n_servers=config.N_SERVERS, load=sc["load"],
                      staleness=sc["staleness"], scenario=name, rep=r,
                      duration=config.DURATION_S, warmup=config.WARMUP_S,
                      **scenario_kwargs(name))
                 for s in strategies for r in range(max(reps, 20))]
    return jobs


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)

    one = sub.add_parser("one", help="simulate a single configuration")
    one.add_argument("--strategy", default="falb")
    one.add_argument("--scenario", default="staleness", choices=["staleness", *config.SCENARIOS])
    one.add_argument("--staleness", type=float, default=None,
                     help="poll interval T (default: 1, or the scenario's own)")
    one.add_argument("--load", type=float, default=None)
    one.add_argument("--servers", type=int, default=config.N_SERVERS)
    one.add_argument("--duration", type=float, default=None)
    one.add_argument("--warmup", type=float, default=None)
    one.add_argument("--rep", type=int, default=0, help="repetition number (selects the seeds)")
    one.add_argument("--a1", type=float, default=config.FALB_A1)
    one.add_argument("--a2", type=float, default=config.FALB_A2)

    # Same 60 s windows as the testbed, many more repetitions: like-for-like,
    # because a short window systematically under-reads the p99.
    p = sub.add_parser("validate", help="n = 5 on the testbed's grid and scenarios")
    p.add_argument("--out", default="results/simulation/validate.csv")
    p.add_argument("--reps", type=int, default=20)
    p.add_argument("--duration", type=float, default=config.DURATION_S)
    p.add_argument("--warmup", type=float, default=config.WARMUP_S)
    p.add_argument("--workers", type=int, default=None)

    p = sub.add_parser("extend", help="ablation of FALB, larger pools, several balancers")
    p.add_argument("--out", default="results/simulation")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--only", nargs="+", choices=["ablation", "pools", "balancers"],
                   default=["ablation", "pools", "balancers"])
    p.add_argument("--workers", type=int, default=None)

    args = ap.parse_args(argv)
    if args.command == "one":
        sc = config.SCENARIOS.get(args.scenario, {})
        event = bool(sc)
        row = simulate(
            args.strategy, args.servers,
            args.load if args.load is not None else sc.get("load", 0.9),
            args.staleness if args.staleness is not None else sc.get("staleness", 1.0),
            scenario=args.scenario,
            duration=args.duration or (config.DURATION_S if event else 600.0),
            warmup=args.warmup or (config.WARMUP_S if event else 60.0),
            rep=args.rep, falb_params={"a1": args.a1, "a2": args.a2},
            **scenario_kwargs(args.scenario))
        modes = {k: v for k, v in row.items() if k.startswith("mode_")}
        print(f"{label(row['strategy'])} [{row['scenario']}], n={row['n_servers']}, "
              f"T={row['staleness']:g} s, load={row['load']:g}: mean {row['mean_ms']:.1f} ms, "
              f"p95 {row['p95_ms']:.1f}, p99 {row['p99_ms']:.1f}, max queue {row['max_queue']}, "
              f"CoV {row['dispatch_cov']:.3f}"
              + (f"  modes {', '.join(f'{k[5:]} {v:.0%}' for k, v in modes.items())}" if modes else ""))
    elif args.command == "validate":
        run_jobs(Path(args.out), grid_jobs((config.N_SERVERS,), config.STRATEGIES,
                                           config.STALENESS_S, config.LOADS, args.reps,
                                           args.duration, args.warmup), args.workers)
    else:
        out = Path(args.out)
        if "ablation" in args.only:
            # Which of FALB's components does the work? (n = 5)
            run_jobs(out / "ablation.csv",
                     grid_jobs((config.N_SERVERS,), ABLATION_STRATEGIES, EXTENDED_STALENESS,
                               config.LOADS, args.reps, 300.0, 30.0), args.workers)
        if "pools" in args.only:
            jobs = [dict(strategy=s, n_servers=n, load=0.9, staleness=T, rep=r,
                         duration=120.0, warmup=20.0)
                    for n in LARGE_POOLS for s in config.STRATEGIES for T in EXTENDED_STALENESS
                    for r in range(args.reps)]
            run_jobs(out / "pools.csv", jobs, args.workers)
        if "balancers" in args.only:
            # Several balancers sharing one snapshot, at 90 % load.
            jobs = [dict(strategy=s, n_servers=config.N_SERVERS, load=0.9, staleness=T, rep=r,
                         duration=300.0, warmup=30.0, balancers=b, shared_snapshot=True)
                    for b in BALANCER_COUNTS for s in (*config.STRATEGIES, "falb_noadapt")
                    for T in EXTENDED_STALENESS for r in range(args.reps)]
            run_jobs(out / "balancers.csv", jobs, args.workers)


if __name__ == "__main__":
    main()
