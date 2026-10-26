"""Metrics recorded for every run (see "Metrics" in the case study)."""

from __future__ import annotations

import math

import numpy as np


def latency_summary(latency_s: np.ndarray, timeout: float) -> dict:
    """Mean and tail latency in ms over all measured requests.

    A request that got no reply within ``timeout`` is counted at the timeout
    value, so the percentiles are lower bounds once timeouts appear (and the
    timeout count is reported alongside them).
    """
    lat = np.asarray(latency_s, dtype=float)
    timed_out = np.isnan(lat) | (lat > timeout)
    censored = np.where(timed_out, timeout, lat) * 1000.0
    if censored.size == 0:
        nan = math.nan
        return dict(n_requests=0, n_ok=0, n_timeout=0, mean_ms=nan, p50_ms=nan,
                    p95_ms=nan, p99_ms=nan, p999_ms=nan, max_ms=nan)
    p50, p95, p99, p999 = np.percentile(censored, [50, 95, 99, 99.9])
    return dict(
        n_requests=int(lat.size),
        n_ok=int((~timed_out).sum()),
        n_timeout=int(timed_out.sum()),
        mean_ms=float(censored.mean()),
        p50_ms=float(p50), p95_ms=float(p95), p99_ms=float(p99), p999_ms=float(p999),
        max_ms=float(censored.max()),
    )


def coefficient_of_variation(counts) -> float:
    """std / mean of requests per server: 0 = perfectly even spread."""
    c = np.asarray(counts, dtype=float)
    return float(c.std() / c.mean()) if c.size and c.mean() > 0 else math.nan


def server_timelines(server_stats: list[dict], window_start: float, duration: float,
                     step: float = 0.001):
    """Each server's busy and waiting state over the window, on a ``step`` grid.

    Servers log (arrival, start, finish) per request relative to their own
    RESET; all processes share one clock, so shifting by the RESET offset
    puts every server on the load generator's window. Returns
    ``(jobs, busy, waiting)``: the shifted job arrays, a (servers x steps)
    bool array "server was processing", and a (servers x steps) int array
    "requests waiting in that server's queue".
    """
    n_steps = int(round(duration / step))
    busy = np.zeros((len(server_stats), n_steps), dtype=bool)
    waiting = np.zeros((len(server_stats), n_steps), dtype=np.int32)
    jobs = []

    def edges(t):
        return np.clip(np.rint(t / step).astype(np.int64), 0, n_steps)

    for k, s in enumerate(server_stats):
        j = np.asarray(s.get("jobs") or [], dtype=float).reshape(-1, 3)
        if s.get("window_start") is not None and j.size:
            j = j + (s["window_start"] - window_start)
        jobs.append(j)
        for row, (begin, end) in ((busy, (1, 2)), (waiting, (0, 1))):
            diff = np.zeros(n_steps + 1, dtype=np.int32)
            np.add.at(diff, edges(j[:, begin]), 1)
            np.add.at(diff, edges(j[:, end]), -1)
            level = np.cumsum(diff)[:-1]
            row[k] = level > 0 if row is busy else level
    return jobs, busy, waiting


def node_summary(server_stats: list[dict], window_start: float, duration: float) -> dict:
    """Per-server utilization, idle time, tasks and memory over the window.

    * busy %: share of the window the server was processing a request. This
      is its CPU utilization: requests replay measured task times, so a busy
      server is one whose CPU would be running the task.
    * idle while others wait %: share of the window a server sat idle while
      at least one request was waiting in another server's queue. Zero means
      no capacity was wasted; it is where uneven routing shows.
    * tasks: requests completed in the window.
    * peak memory: the server process's largest resident set (OS-measured).
    * OS CPU %: the process's real CPU time, i.e. its request-handling
      overhead (the replayed task itself does not use the CPU).
    """
    jobs, busy, waiting = server_timelines(server_stats, window_start, duration)
    busy_pct, tasks, peak_mb, os_cpu = [], [], [], []
    for j, s in zip(jobs, server_stats):
        start = np.clip(j[:, 1], 0, duration)
        finish = np.clip(j[:, 2], 0, duration)
        busy_pct.append(100.0 * float((finish - start).sum()) / duration)
        tasks.append(int(((j[:, 2] >= 0) & (j[:, 2] < duration)).sum()))
        res = np.asarray(s.get("resources") or [], dtype=float).reshape(-1, 3)
        if res.size:
            res[:, 0] += s["window_start"] - window_start
            inside = res[(res[:, 0] >= 0) & (res[:, 0] <= duration)]
            res = inside if len(inside) >= 2 else res
            peak_mb.append(float(res[:, 1].max()) / 2**20)
            span = res[-1, 0] - res[0, 0]
            os_cpu.append(100.0 * (res[-1, 2] - res[0, 2]) / span if span > 0 else math.nan)
    queued_somewhere = waiting.sum(axis=0) > 0
    idle_wait = 100.0 * float((~busy & queued_somewhere).mean()) if busy.size else math.nan

    def joined(values, digits=1):
        return "|".join(f"{v:.{digits}f}" for v in values)

    return {
        "busy_pct_mean": float(np.mean(busy_pct)) if busy_pct else math.nan,
        "busy_pct_min": float(np.min(busy_pct)) if busy_pct else math.nan,
        "busy_pct_max": float(np.max(busy_pct)) if busy_pct else math.nan,
        "idle_pct_mean": 100.0 - float(np.mean(busy_pct)) if busy_pct else math.nan,
        "idle_while_waiting_pct": idle_wait,
        "busy_pct_per_server": joined(busy_pct),
        "tasks_per_server": "|".join(map(str, tasks)),
        "tasks_cov": coefficient_of_variation(tasks),
        "peak_mem_mb_max": max(peak_mb) if peak_mb else math.nan,
        "peak_mem_mb_per_server": joined(peak_mb),
        "os_cpu_pct_mean": float(np.nanmean(os_cpu)) if os_cpu else math.nan,
        "os_cpu_pct_per_server": joined(os_cpu),
    }


def utilization_per_second(busy: np.ndarray, step: float = 0.001) -> np.ndarray:
    """(servers x seconds) busy % in each whole second of the window."""
    per_s = int(round(1 / step))
    n = busy.shape[1] // per_s
    return 100.0 * busy[:, :n * per_s].reshape(busy.shape[0], n, per_s).mean(axis=2)


def mm1_mean_ms(load: float, mean_service: float) -> float:
    """Mean response time of an M/M/1 queue: what Random should achieve."""
    return 1000.0 * mean_service / (1.0 - load)


def mm1_percentile_ms(load: float, mean_service: float, q: float) -> float:
    """M/M/1 response time is exponential with rate mu(1 - rho)."""
    return -math.log(1.0 - q / 100.0) * mm1_mean_ms(load, mean_service)
