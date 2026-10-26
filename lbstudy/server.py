"""Backend server: one worker serving a FIFO queue with exponential job times.

Each server processes one request at a time. Processing times are drawn from
an exponential distribution (mean 20 ms by default, so ~50 requests/s), which
makes every server an M/M/1-style queue. With ``--tasks`` a request that
names a task class (fast, moderate, slow) instead takes the time of one
randomly picked measurement of that class (see ``lbstudy.tasks``).

Status reports (STATUS -> STATUS_REPLY) carry four numbers:
  queue      requests held right now, waiting or in service
  time       when the report was produced (wall clock), so its age is known
  completed  requests finished since the server started
  busy       seconds spent processing since the server started
From two reports a load balancer can work out the processing rate
(completed / busy) and the CPU utilization (busy / elapsed time). In this
testbed the "CPU" is the server's single worker. Jobs are simulated by
sleeping, so the busy fraction is exactly what a CPU-bound server's
utilization would be, while the Python process's real CPU usage would only
measure interpreter overhead.

For the measurement window (from RESET on), the server also logs when each
request arrived, started and finished, and samples its own memory use and
OS-measured CPU time twice a second. The runner turns these into per-server
utilization, idle time and memory.

Scenario events arrive as CONTROL messages from the experiment runner:
  {"mute": seconds}                    stop answering status polls
  {"slow": factor, "duration": secs}   multiply processing times

Run standalone:
    python -m lbstudy.server --id 0 --port 9001
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from collections import deque
from typing import Sequence

try:
    import psutil
except ImportError:            # memory and OS CPU are then simply not reported
    psutil = None

from . import config, tasks
from .platform_tuning import run
from .protocol import STATUS, U16, Msg, encode, read_frame, set_nodelay


class BackendServer:
    def __init__(self, server_id: int, mean_service: float = config.MEAN_SERVICE_S,
                 seed: int | None = None, sample_interval: float = 0.05,
                 task_times: Sequence[Sequence[float]] | None = None):
        self.server_id = server_id
        self.mean_service = mean_service
        self.task_times = [list(t) for t in task_times] if task_times else None
        self._rng = random.Random(seed)
        self._jobs: asyncio.Queue = asyncio.Queue()
        self._reply = U16.pack(server_id)

        self.queue_length = 0       # requests held: waiting + in service
        self.received = 0
        self.completed = 0
        self.busy_done = 0.0        # processing seconds of completed requests
        self._job_start = None      # (start, finish) of the request in service
        self.max_queue = 0          # since the last RESET (measurement window)

        self._muted_until = 0.0     # loop time until which STATUS is ignored
        self._slow_factor = 1.0
        self._slow_until = 0.0
        self.events: list[dict] = []

        # Queue-length trace for plotting: one (unix time, max queue length
        # during the interval) sample per ``sample_interval`` seconds.
        self._sample_interval = sample_interval
        self._interval_max = 0
        self.trace: deque[tuple[float, int]] = deque(maxlen=200_000)
        self._tasks: list[asyncio.Task] = []

        # Measurement window: per-request (arrival, start, finish) times and
        # (time, memory bytes, CPU seconds) samples, relative to RESET.
        self.window_start: float | None = None
        self.jobs: list[tuple[float, float, float]] = []
        self.resources: list[tuple[float, int, float]] = []
        self._process = psutil.Process() if psutil else None

    async def start(self, host: str, port: int) -> asyncio.Server:
        self._tasks = [asyncio.create_task(self._work()),
                       asyncio.create_task(self._sample()),
                       asyncio.create_task(self._sample_resources())]
        return await asyncio.start_server(self._handle, host, port)

    def status(self) -> bytes:
        loop_now = asyncio.get_running_loop().time()
        busy = self.busy_done
        if self._job_start is not None:
            start, finish = self._job_start
            busy += max(0.0, min(loop_now, finish) - start)
        return STATUS.pack(self.queue_length, time.time(), self.completed, busy)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        set_nodelay(writer)
        loop = asyncio.get_running_loop()
        try:
            while True:
                kind, req_id, payload = await read_frame(reader)
                if kind == Msg.REQUEST:
                    task_class = payload[0] if payload else -1
                    self.received += 1
                    self.queue_length += 1
                    if self.queue_length > self.max_queue:
                        self.max_queue = self.queue_length
                    if self.queue_length > self._interval_max:
                        self._interval_max = self.queue_length
                    self._jobs.put_nowait((loop.time(), writer, req_id, task_class))
                elif kind == Msg.STATUS:
                    if loop.time() >= self._muted_until:      # silent while muted
                        writer.write(encode(Msg.STATUS_REPLY, req_id, self.status()))
                elif kind == Msg.STATS:
                    writer.write(encode(Msg.STATS_REPLY, req_id, json.dumps(self.stats()).encode()))
                elif kind == Msg.RESET:
                    self.max_queue = self.queue_length
                    self.window_start = loop.time()
                    self.jobs = []
                    self.resources = []
                    self._record_resources(self.window_start)
                    writer.write(encode(Msg.RESET_REPLY, req_id))
                elif kind == Msg.CONTROL:
                    self.control(json.loads(payload), loop.time())
                    writer.write(encode(Msg.CONTROL_REPLY, req_id))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            writer.close()

    def control(self, event: dict, now: float) -> None:
        if "mute" in event:
            self._muted_until = now + float(event["mute"])
        if "slow" in event:
            self._slow_factor = float(event["slow"])
            self._slow_until = now + float(event.get("duration", 1e12))
        self.events.append({"unix_time": time.time(), **event})

    async def _work(self) -> None:
        loop = asyncio.get_running_loop()
        finish = 0.0
        while True:
            arrived, writer, req_id, task_class = await self._jobs.get()
            # Service starts when the job arrives or when the previous job
            # finishes, whichever is later. Sleeping until that absolute
            # deadline, instead of for a duration, means a late timer wake-up
            # delays only this reply and never slows the server's true
            # service rate, which stays exactly 1 / mean_service.
            start = max(arrived, finish)
            service = self._service_time(task_class)
            if start < self._slow_until:
                service *= self._slow_factor
            finish = start + service
            self._job_start = (start, finish)
            delay = finish - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._job_start = None
            self.busy_done += service
            self.queue_length -= 1
            self.completed += 1
            if self.window_start is not None:
                ws = self.window_start
                self.jobs.append((arrived - ws, start - ws, finish - ws))
            if not writer.is_closing():
                writer.write(encode(Msg.RESPONSE, req_id, self._reply))

    def _service_time(self, task_class: int) -> float:
        if self.task_times is not None and 0 <= task_class < len(self.task_times):
            return self._rng.choice(self.task_times[task_class])
        return self._rng.expovariate(1.0 / self.mean_service)

    def _record_resources(self, now: float) -> None:
        if self._process is None or self.window_start is None:
            return
        cpu = self._process.cpu_times()
        self.resources.append((now - self.window_start, self._process.memory_info().rss,
                               cpu.user + cpu.system))

    async def _sample_resources(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(0.5)
            self._record_resources(loop.time())

    async def _sample(self) -> None:
        while True:
            await asyncio.sleep(self._sample_interval)
            self.trace.append((round(time.time(), 4), self._interval_max))
            self._interval_max = self.queue_length

    def stats(self) -> dict:
        return {
            "server_id": self.server_id,
            "received": self.received,
            "completed": self.completed,
            "queue_length": self.queue_length,
            "max_queue": self.max_queue,
            "events": self.events,
            "trace": list(self.trace),
            "window_start": self.window_start,
            "jobs": [round(x, 6) for job in self.jobs for x in job],
            "resources": [[round(t, 3), rss, round(cpu, 4)] for t, rss, cpu in self.resources],
        }


async def serve(server_id: int, host: str, port: int, mean_service: float,
                seed: int | None, tasks_file: str | None = None) -> None:
    task_times = tasks.load(tasks_file).times_s if tasks_file else None
    backend = BackendServer(server_id, mean_service, seed, task_times=task_times)
    listener = await backend.start(host, port)
    actual_port = listener.sockets[0].getsockname()[1]
    # The experiment runner waits for this line to learn the (possibly
    # OS-assigned) port. Nothing else may be printed to stdout.
    print(f"READY {actual_port}", flush=True)
    async with listener:
        await listener.serve_forever()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--id", type=int, default=0, help="server id reported in replies")
    ap.add_argument("--host", default=config.HOST)
    ap.add_argument("--port", type=int, default=9001, help="0 = let the OS choose")
    ap.add_argument("--mean-service", type=float, default=config.MEAN_SERVICE_S,
                    help="mean processing time in seconds (default 0.020)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--tasks", default=None,
                    help="measured task times (results/tasks.json) for class-tagged requests")
    args = ap.parse_args(argv)
    try:
        run(serve(args.id, args.host, args.port, args.mean_service, args.seed, args.tasks))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
