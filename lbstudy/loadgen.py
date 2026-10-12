"""Open-loop Poisson load generator.

Requests are sent at exponentially distributed intervals, on schedule, no
matter how many earlier requests are still waiting. That is what lets queues
build up as they would under real traffic: a closed-loop client slows down
when the service slows down and so hides the very delays being measured.

Latency is measured from each request's *scheduled* send time, so any delay
inside the generator itself counts against the system instead of silently
vanishing (avoiding "coordinated omission").

Run standalone against a running balancer:
    python -m lbstudy.loadgen --port 9000 --rate 200 --duration 20
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import math
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence

import numpy as np

from . import config
from .platform_tuning import run
from .protocol import U16, Link, Msg


@dataclass
class LoadResult:
    scheduled: np.ndarray   # send time, s, relative to window start (< 0: warm-up)
    latency: np.ndarray     # s from scheduled send to reply; NaN = no reply
    server: np.ndarray      # id of the server that answered; -1 = none
    send_lag: np.ndarray    # actual minus scheduled send time, s
    window_start_unix: float
    rate: float
    duration: float
    timeout: float

    @property
    def measured(self) -> np.ndarray:
        return (self.scheduled >= 0) & (self.scheduled < self.duration)


async def generate(host: str, port: int | Sequence[int], rate: float, duration: float,
                   warmup: float = 0.0,
                   timeout: float = config.TIMEOUT_S, seed: int | None = None,
                   on_window_start: Callable[[], Awaitable[None]] | None = None) -> LoadResult:
    """Send Poisson traffic for ``warmup + duration`` seconds, then drain.

    Requests scheduled during the warm-up are sent but not measured. After
    the last send, waits until every measured request is answered or
    ``timeout`` seconds have passed since the end of the window. With
    several balancer ports, each request goes to one of them at random.
    """
    loop = asyncio.get_running_loop()
    rng = random.Random(seed)
    spread = random.Random(None if seed is None else seed + 1)
    ports = [port] if isinstance(port, int) else list(port)
    links = [await Link.open(host, p) for p in ports]

    scheduled: list[float] = []
    latency: list[float] = []
    server: list[int] = []
    lag: list[float] = []
    measured_replies: list[asyncio.Future] = []

    def on_reply(i: int, reply: asyncio.Future) -> None:
        if reply.cancelled() or reply.exception() is not None:
            return
        kind, payload = reply.result()
        if kind == Msg.RESPONSE:
            latency[i] = loop.time() - scheduled[i]
            server[i] = U16.unpack(payload)[0]

    start = loop.time() + 0.05
    window_start = start + warmup
    end = window_start + duration
    unix_offset = time.time() - loop.time()

    window_task: list[asyncio.Task] = []
    if on_window_start is not None:
        loop.call_at(window_start, lambda: window_task.append(loop.create_task(on_window_start())))

    t = start
    while True:
        t += rng.expovariate(rate)
        if t >= end:
            break
        delay = t - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        i = len(scheduled)
        scheduled.append(t)
        latency.append(math.nan)
        server.append(-1)
        lag.append(loop.time() - t)
        link = links[spread.randrange(len(links))] if len(links) > 1 else links[0]
        reply = link.send(Msg.REQUEST)
        reply.add_done_callback(functools.partial(on_reply, i))
        if t >= window_start:
            measured_replies.append(reply)

    remaining = end + timeout - loop.time()
    if measured_replies and remaining > 0:
        await asyncio.wait(measured_replies, timeout=remaining)
    for task in window_task:
        await task
    for link in links:
        await link.close()

    return LoadResult(
        scheduled=np.asarray(scheduled) - window_start,
        latency=np.asarray(latency),
        server=np.asarray(server, dtype=np.int16),
        send_lag=np.asarray(lag),
        window_start_unix=window_start + unix_offset,
        rate=rate, duration=duration, timeout=timeout,
    )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default=config.HOST)
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--rate", type=float, required=True, help="requests per second")
    ap.add_argument("--duration", type=float, default=20.0)
    ap.add_argument("--warmup", type=float, default=2.0)
    ap.add_argument("--timeout", type=float, default=config.TIMEOUT_S)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args(argv)

    result = run(generate(args.host, args.port, args.rate, args.duration, args.warmup,
                          args.timeout, args.seed))
    lat = result.latency[result.measured] * 1000
    ok = lat[~np.isnan(lat)]
    print(f"sent {lat.size} measured requests, {ok.size} answered")
    if ok.size:
        print(f"mean {ok.mean():.1f} ms   p95 {np.percentile(ok, 95):.1f} ms   "
              f"p99 {np.percentile(ok, 99):.1f} ms   max {ok.max():.1f} ms")


if __name__ == "__main__":
    main()
