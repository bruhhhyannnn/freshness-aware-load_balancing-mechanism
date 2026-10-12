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


def mm1_mean_ms(load: float, mean_service: float) -> float:
    """Mean response time of an M/M/1 queue: what Random should achieve."""
    return 1000.0 * mean_service / (1.0 - load)


def mm1_percentile_ms(load: float, mean_service: float, q: float) -> float:
    """M/M/1 response time is exponential with rate mu(1 - rho)."""
    return -math.log(1.0 - q / 100.0) * mm1_mean_ms(load, mean_service)
