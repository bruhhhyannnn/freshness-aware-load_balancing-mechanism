"""Load balancer: forwards each request to a backend chosen by a strategy.

The age of the load information is controlled exactly. With staleness
``T > 0`` the balancer polls every server's STATUS every ``T`` seconds and
hands each report to the strategy. The baselines decide from those reports
alone, the "periodic update" model of Mitzenmacher, "How useful is old
information?" (2000). FALB also counts the requests it has sent since.

With ``T == 0`` the balancer asks the candidate servers for a report when
each request arrives: every server for Least-Loaded and FALB, the two
sampled servers for the Power of Two Choices. Round Robin and Random never
look at load, so ``T`` does not affect how they route.

A server that does not answer a poll within the probe timeout keeps its old
report. Its information simply ages, which is the missing-report scenario.

With ``--aligned-polls`` the polls happen at wall-clock multiples of T, so
several balancers started this way all see the same snapshot at the same
moment (like balancers fed by one central health checker).

Run standalone (after starting servers on ports 9001-9005):
    python -m lbstudy.balancer --port 9000 --strategy falb --staleness 1 \\
        --backends 127.0.0.1:9001,127.0.0.1:9002,127.0.0.1:9003,127.0.0.1:9004,127.0.0.1:9005
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import math
import random
import time

from . import config
from .platform_tuning import run
from .protocol import STATUS, Link, Msg, encode, read_frame, set_nodelay
from .strategies import Report, Strategy, make_strategy


class LoadBalancer:
    def __init__(self, backends: list[tuple[str, int]], strategy: Strategy, staleness: float,
                 aligned_polls: bool = False):
        if staleness < 0:
            raise ValueError("staleness must be >= 0")
        if strategy.n != len(backends):
            raise ValueError("strategy and backend list disagree on server count")
        self.backends = backends
        self.strategy = strategy
        self.staleness = staleness
        self.aligned_polls = aligned_polls
        # A status reply should take well under a millisecond; one that has not
        # arrived after this long is treated as missing.
        self.probe_timeout = 0.05 if staleness == 0 else min(0.25, max(0.05, staleness / 2))
        self.links: list[Link] = []
        self._tasks: set[asyncio.Task] = set()
        self.reset_window()

    def reset_window(self) -> None:
        self.dispatched = [0] * len(self.backends)
        self.polls = 0
        self.probes = 0
        self.missed_probes = 0
        self._probe_time = 0.0
        self._decisions = 0
        self._decision_ns = 0
        self._decision_max_ns = 0
        self.strategy.reset_stats()

    async def start(self, host: str, port: int) -> asyncio.Server:
        self.links = [await Link.open(h, p) for h, p in self.backends]
        if self.strategy.uses_load and self.staleness > 0:
            await self._poll()
            self._spawn(self._poll_forever())
        return await asyncio.start_server(self._handle_client, host, port)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)          # keep a reference until it finishes
        task.add_done_callback(self._tasks.discard)

    # --- load information ---------------------------------------------------

    async def _probe(self, i: int) -> None:
        """Ask server i for a status report and pass it to the strategy."""
        loop = asyncio.get_running_loop()
        sent = loop.time()
        try:
            _, payload = await asyncio.wait_for(self.links[i].call(Msg.STATUS),
                                                self.probe_timeout)
        except (asyncio.TimeoutError, ConnectionError):
            self.missed_probes += 1
            return
        self.probes += 1
        self._probe_time += loop.time() - sent
        queue, produced, completed, busy = STATUS.unpack(payload)
        self.strategy.on_report(i, Report(queue, produced, completed, busy), time.time())

    async def _poll(self) -> None:
        await asyncio.gather(*(self._probe(i) for i in range(len(self.links))))
        self.polls += 1

    async def _poll_forever(self) -> None:
        loop = asyncio.get_running_loop()
        T = self.staleness
        next_poll = loop.time()
        while True:
            if self.aligned_polls:
                now = time.time()
                await asyncio.sleep(math.floor(now / T + 1) * T - now)
            else:
                next_poll += T
                delay = next_poll - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    next_poll = loop.time()    # fell behind: do not burst to catch up
            await self._poll()

    # --- request routing ----------------------------------------------------

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        set_nodelay(writer)
        try:
            while True:
                kind, req_id, _ = await read_frame(reader)
                if kind == Msg.REQUEST:
                    self._route(writer, req_id)
                elif kind == Msg.STATS:
                    writer.write(encode(Msg.STATS_REPLY, req_id, json.dumps(self.stats()).encode()))
                elif kind == Msg.RESET:
                    self.reset_window()
                    writer.write(encode(Msg.RESET_REPLY, req_id))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            writer.close()

    def _route(self, writer: asyncio.StreamWriter, req_id: int) -> None:
        if self.strategy.uses_load and self.staleness == 0:
            self._spawn(self._route_fresh(writer, req_id))
            return
        began = time.perf_counter_ns()
        candidates = self.strategy.candidates()
        target = self.strategy.pick(candidates, time.time())
        self._decided(began)
        self._forward(target, writer, req_id)

    async def _route_fresh(self, writer, req_id) -> None:
        candidates = self.strategy.candidates()
        await asyncio.gather(*(self._probe(i) for i in candidates))
        began = time.perf_counter_ns()
        target = self.strategy.pick(candidates, time.time())
        self._decided(began)
        self._forward(target, writer, req_id)

    def _decided(self, began_ns: int) -> None:
        took = time.perf_counter_ns() - began_ns
        self._decisions += 1
        self._decision_ns += took
        if took > self._decision_max_ns:
            self._decision_max_ns = took

    def _forward(self, i: int, writer: asyncio.StreamWriter, req_id: int) -> None:
        self.dispatched[i] += 1
        self.strategy.on_dispatch(i, time.time())
        reply = self.links[i].send(Msg.REQUEST)
        reply.add_done_callback(functools.partial(_relay, writer, req_id))

    def stats(self) -> dict:
        return {
            "strategy": self.strategy.name,
            "staleness": self.staleness,
            "dispatched": self.dispatched,
            "polls": self.polls,
            "probes": self.probes,
            "missed_probes": self.missed_probes,
            "probe_rtt_ms": 1000 * self._probe_time / self.probes if self.probes else None,
            "decisions": self._decisions,
            "decision_us": self._decision_ns / 1000 / self._decisions if self._decisions else None,
            "decision_max_us": self._decision_max_ns / 1000,
            **self.strategy.stats(),
        }


def _relay(writer: asyncio.StreamWriter, req_id: int, reply: asyncio.Future) -> None:
    """Pass a backend's reply back to the client under the client's own id."""
    if writer.is_closing():
        return
    if reply.cancelled() or reply.exception() is not None:
        writer.write(encode(Msg.ERROR, req_id))
        return
    _, payload = reply.result()
    writer.write(encode(Msg.RESPONSE, req_id, payload))


def parse_backends(spec: str) -> list[tuple[str, int]]:
    backends = []
    for item in spec.split(","):
        host, _, port = item.strip().rpartition(":")
        backends.append((host or config.HOST, int(port)))
    return backends


async def serve(backends, strategy_name: str, staleness: float, host: str, port: int,
                seed: int | None, aligned_polls: bool, falb_params: dict) -> None:
    params = falb_params if strategy_name.startswith("falb") else {}
    strategy = make_strategy(strategy_name, len(backends), random.Random(seed), **params)
    balancer = LoadBalancer(backends, strategy, staleness, aligned_polls)
    listener = await balancer.start(host, port)
    print(f"READY {listener.sockets[0].getsockname()[1]}", flush=True)
    async with listener:
        await listener.serve_forever()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--backends", required=True, help="comma-separated host:port list")
    ap.add_argument("--strategy", default="falb",
                    help="round_robin | random | least_loaded | p2c | falb | falb_cpu | ...")
    ap.add_argument("--staleness", type=float, default=0.0,
                    help="seconds between load polls; 0 = probe per request")
    ap.add_argument("--a1", type=float, default=config.FALB_A1, help="FALB: fresh-age limit (s)")
    ap.add_argument("--a2", type=float, default=config.FALB_A2, help="FALB: timeout (s)")
    ap.add_argument("--aligned-polls", action="store_true",
                    help="poll at wall-clock multiples of T (shared snapshot across balancers)")
    ap.add_argument("--host", default=config.HOST)
    ap.add_argument("--port", type=int, default=9000, help="0 = let the OS choose")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args(argv)
    try:
        run(serve(parse_backends(args.backends), args.strategy, args.staleness,
                  args.host, args.port, args.seed, args.aligned_polls,
                  {"a1": args.a1, "a2": args.a2}))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
