"""Load-balancing strategies: the four baselines and the proposed FALB.

The load balancer drives every strategy through the same four calls:

* ``on_report(i, report, now)``: a status report from server ``i`` arrived.
* ``candidates()``: which servers the strategy wants to look at for the next
  request (with T = 0 the balancer probes exactly these servers first).
* ``pick(candidates, now)``: choose the server for the request.
* ``on_dispatch(i, now)``: the request was sent to server ``i``.

The baselines only remember the last report of each server. FALB also keeps
its own record of what it has sent. The same classes drive both the real
asyncio testbed and the discrete-event simulator, so both use identical
decision logic.
"""

from __future__ import annotations

import math
import random
import re
from collections import deque
from dataclasses import dataclass
from typing import Sequence

from . import config


@dataclass(frozen=True, slots=True)
class Report:
    """One status report, as produced by a server."""
    queue: int          # requests held right now (waiting + in service)
    time: float         # when the report was produced (seconds, wall clock)
    completed: int      # requests finished since the server started
    busy: float         # seconds spent processing since the server started


class Strategy:
    name = ""
    uses_load = False

    def __init__(self, n_servers: int, rng: random.Random | None = None):
        if n_servers < 1:
            raise ValueError("need at least one server")
        self.n = n_servers
        self.rng = rng or random.Random()
        self.reports: list[Report | None] = [None] * n_servers

    def candidates(self) -> Sequence[int]:
        raise NotImplementedError

    def pick(self, candidates: Sequence[int], now: float) -> int:
        return candidates[0]

    def on_report(self, i: int, report: Report, now: float) -> None:
        self.reports[i] = report

    def on_dispatch(self, i: int, now: float) -> None:
        pass

    def stats(self) -> dict:
        return {}

    def reset_stats(self) -> None:
        pass

    def _reported(self, i: int) -> int:
        report = self.reports[i]
        return report.queue if report is not None else 0

    def _least(self, candidates: Sequence[int], loads: Sequence[float]) -> int:
        """The candidate with the lowest load, ties broken at random.

        Random tie-breaking matters: with a fixed order (say, lowest index
        first) every tie would send traffic to the same server and add a
        herding effect that has nothing to do with stale information.
        """
        best = min(loads)
        tied = [c for c, load in zip(candidates, loads) if load == best]
        return tied[0] if len(tied) == 1 else self.rng.choice(tied)

    def _two(self, population: Sequence[int]) -> Sequence[int]:
        """Two distinct servers drawn uniformly at random."""
        k = len(population)
        if k <= 2:
            return population
        a = self.rng.randrange(k)
        b = self.rng.randrange(k - 1)
        return (population[a], population[b + (b >= a)])


class RoundRobin(Strategy):
    """Servers take turns: 0, 1, 2, ..., n-1, 0, 1, ..."""

    name = "round_robin"

    def __init__(self, n_servers: int, rng: random.Random | None = None):
        super().__init__(n_servers, rng)
        self._next = 0

    def candidates(self) -> Sequence[int]:
        i = self._next
        self._next = (i + 1) % self.n
        return (i,)


class RandomChoice(Strategy):
    """Each request goes to a uniformly random server."""

    name = "random"

    def candidates(self) -> Sequence[int]:
        return (self.rng.randrange(self.n),)


class LeastLoaded(Strategy):
    """Look at every server; pick the lowest reported queue length."""

    name = "least_loaded"
    uses_load = True

    def __init__(self, n_servers: int, rng: random.Random | None = None):
        super().__init__(n_servers, rng)
        self._all = tuple(range(n_servers))

    def candidates(self) -> Sequence[int]:
        return self._all

    def pick(self, candidates, now):
        return self._least(candidates, [self._reported(i) for i in candidates])


class PowerOfD(Strategy):
    """Sample d distinct servers at random; pick the lowest reported queue.

    d = 2 is the Power of Two Choices (Mitzenmacher's supermarket model).
    """

    uses_load = True

    def __init__(self, n_servers: int, rng: random.Random | None = None, d: int = 2):
        super().__init__(n_servers, rng)
        if not 1 <= d <= n_servers:
            raise ValueError(f"d must be between 1 and {n_servers}, got {d}")
        self.d = d
        self.name = f"p{d}c"
        self._all = tuple(range(n_servers))

    def candidates(self) -> Sequence[int]:
        if self.d == 2:
            return self._two(self._all)
        return self.rng.sample(self._all, self.d)

    def pick(self, candidates, now):
        return self._least(candidates, [self._reported(i) for i in candidates])


class FreshnessAware(Strategy):
    """Freshness-Aware Load Balancer (FALB), the mechanism proposed in the study.

    1. Age tracking. Every report carries the time it was produced, so the
       age of each server's information is ``now - report.time``.
    2. Stale correction. Between reports FALB keeps its own estimate of each
       queue: it starts at the reported length, rises by one for every request
       FALB sends to that server, and falls at the server's measured
       processing rate, never below zero at any moment. A server that
       reported an empty queue stops looking empty as soon as requests are
       sent to it, which is what defeats herding.
    3. Adaptive choice with timeout. If every report is younger than A1, FALB
       compares all servers. If the information is older than A1, it compares
       two random servers. A server whose report is older than the timeout A2
       is not trusted at all: it simply receives its equal share (1/n) of
       requests at random. When every report is that old, FALB is pure Random.

    The processing rate is measured from the reports themselves (requests
    completed / seconds busy), as an exponentially weighted average over
    roughly the last ``rate_window`` completed requests, so a server that
    slows down is soon treated as slower.

    Variants for the ablation study:
      correct=False   no stale correction (uses the raw reported queue)
      adaptive=False  always compares all servers, never times out
      signal="cpu"    uses CPU utilization u (busy fraction over the last
                      second) instead of queue length, converted to an
                      equivalent queue length with the M/M/1 relation u/(1-u)
      fallback="turns" servers whose reports timed out take turns instead of
                      being picked at random (still 1/n of the traffic each)
    """

    uses_load = True

    def __init__(self, n_servers: int, rng: random.Random | None = None, *,
                 a1: float = config.FALB_A1, a2: float = config.FALB_A2,
                 correct: bool = True, adaptive: bool = True, signal: str = "queue",
                 fallback: str = "random", rate_window: int = 100, name: str = "falb"):
        super().__init__(n_servers, rng)
        if not 0 <= a1 <= a2:
            raise ValueError("need 0 <= A1 <= A2")
        if signal not in ("queue", "cpu"):
            raise ValueError("signal must be 'queue' or 'cpu'")
        if fallback not in ("random", "turns"):
            raise ValueError("fallback must be 'random' or 'turns'")
        self.fallback = fallback
        self._turn = 0
        self.name = name
        self.a1, self.a2 = a1, a2
        self.correct, self.adaptive, self.signal = correct, adaptive, signal
        self._all = tuple(range(n_servers))
        self._est = [0.0] * n_servers        # estimated queue length ...
        self._est_time = [0.0] * n_servers   # ... as of this time
        self._decay = 1.0 - 1.0 / rate_window
        self._done = [0.0] * n_servers       # decayed sums for the rate estimate
        self._busy = [0.0] * n_servers
        self._history = [deque() for _ in range(n_servers)]   # (time, busy) for CPU
        self._report_time = [-math.inf] * n_servers
        self.reset_stats()

    # --- information --------------------------------------------------------

    def on_report(self, i: int, report: Report, now: float) -> None:
        prev = self.reports[i]
        if prev is not None and report.completed > prev.completed and report.busy > prev.busy:
            d = self._decay ** (report.completed - prev.completed)
            self._done[i] = self._done[i] * d + (report.completed - prev.completed)
            self._busy[i] = self._busy[i] * d + (report.busy - prev.busy)
        self.reports[i] = report
        self._report_time[i] = report.time
        self._est[i] = float(report.queue) if self.signal == "queue" else self._cpu_load(i, report)
        self._est_time[i] = report.time

    def _cpu_load(self, i: int, report: Report) -> float:
        history = self._history[i]
        history.append((report.time, report.busy))
        while len(history) > 2 and report.time - history[1][0] >= 1.0:
            history.popleft()
        t0, b0 = history[0]
        u = (report.busy - b0) / (report.time - t0) if report.time > t0 else 0.0
        u = min(max(u, 0.0), 0.98)
        return u / (1.0 - u)

    def rate(self, i: int) -> float | None:
        """Measured processing rate of server i (requests/s), if known yet."""
        return self._done[i] / self._busy[i] if self._busy[i] > 0 else None

    def estimate(self, i: int, now: float) -> float:
        if not self.correct:
            return self._est[i]
        rate = self.rate(i)
        if rate is not None and now > self._est_time[i]:
            self._est[i] = max(0.0, self._est[i] - rate * (now - self._est_time[i]))
            self._est_time[i] = now
        return self._est[i]

    def on_dispatch(self, i: int, now: float) -> None:
        if self.correct:
            self.estimate(i, now)
            self._est[i] += 1.0

    # --- decision -----------------------------------------------------------

    def candidates(self) -> Sequence[int]:
        return self._all

    def pick(self, candidates, now):
        usable = self._all
        if self.adaptive:
            oldest = min(self._report_time)
            if now - oldest > self.a2:                     # some report timed out
                times = self._report_time
                expired = [i for i in self._all if now - times[i] > self.a2]
                if len(expired) == self.n or self.rng.random() < len(expired) / self.n:
                    self.modes["random"] += 1
                    if self.fallback == "turns":
                        self._turn += 1
                        return expired[self._turn % len(expired)]
                    return self.rng.choice(expired)
                usable = tuple(i for i in self._all if now - times[i] <= self.a2)
                oldest = min(times[i] for i in usable)
            if now - oldest > self.a1:
                self.modes["two"] += 1
                chosen = self._two(usable)
                return self._least(chosen, [self.estimate(i, now) for i in chosen])
        self.modes["all"] += 1
        return self._least(usable, [self.estimate(i, now) for i in usable])

    def stats(self) -> dict:
        total = sum(self.modes.values()) or 1
        return {f"mode_{k}": v / total for k, v in self.modes.items()}

    def reset_stats(self) -> None:
        self.modes = {"all": 0, "two": 0, "random": 0}


LABELS = {
    "round_robin": "Round Robin",
    "random": "Random",
    "least_loaded": "Least-Loaded",
    "p2c": "Power of Two Choices",
    "p3c": "Power of Three Choices",
    "falb": "FALB (proposed)",
    "falb_cpu": "FALB, CPU signal",
    "falb_nocorr": "FALB without correction",
    "falb_noadapt": "FALB without adaptive choice",
    "falb_turns": "FALB, turn-taking fallback",
}

FALB_VARIANTS = {
    "falb": {},
    "falb_cpu": {"signal": "cpu"},
    "falb_nocorr": {"correct": False},
    "falb_noadapt": {"adaptive": False},
    "falb_turns": {"fallback": "turns"},
}


def label(name: str) -> str:
    m = re.fullmatch(r"p(\d+)c", name)
    return LABELS.get(name) or (f"Power of {m.group(1)} Choices" if m else name)


def make_strategy(name: str, n_servers: int, rng: random.Random | None = None,
                  **falb_params) -> Strategy:
    """Build a strategy by name.

    Names: round_robin, random, least_loaded, p2c, p3c, ..., falb, falb_cpu,
    falb_nocorr, falb_noadapt. ``falb_params`` (a1, a2, ...) apply to FALB only.
    """
    key = name.lower().replace("-", "_")
    if key in ("round_robin", "rr"):
        return RoundRobin(n_servers, rng)
    if key == "random":
        return RandomChoice(n_servers, rng)
    if key in ("least_loaded", "least", "ll", "jsq"):
        return LeastLoaded(n_servers, rng)
    if key in FALB_VARIANTS:
        return FreshnessAware(n_servers, rng, name=key, **{**FALB_VARIANTS[key], **falb_params})
    m = re.fullmatch(r"p(\d+)c", key)
    if m:
        return PowerOfD(n_servers, rng, d=int(m.group(1)))
    raise ValueError(f"unknown strategy {name!r}; use round_robin, random, least_loaded, "
                     f"p2c, p3c, {', '.join(FALB_VARIANTS)}")
