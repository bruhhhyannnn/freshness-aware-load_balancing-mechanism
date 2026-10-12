"""Backend server: one worker serving a FIFO queue with exponential job times.

Each server processes one request at a time. Processing times are drawn from
an exponential distribution (mean 20 ms by default, so ~50 requests/s), which
makes every server an M/M/1-style queue.

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

from . import config
from .platform_tuning import run
from .protocol import STATUS, U16, Msg, encode, read_frame, set_nodelay


class BackendServer:
    def __init__(self, server_id: int, mean_service: float = config.MEAN_SERVICE_S,
                 seed: int | None = None, sample_interval: float = 0.05):
        self.server_id = server_id
        self.mean_service = mean_service
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

    async def start(self, host: str, port: int) -> asyncio.Server:
        self._tasks = [asyncio.create_task(self._work()),
                       asyncio.create_task(self._sample())]
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
                    self.received += 1
                    self.queue_length += 1
                    if self.queue_length > self.max_queue:
                        self.max_queue = self.queue_length
                    if self.queue_length > self._interval_max:
                        self._interval_max = self.queue_length
                    self._jobs.put_nowait((loop.time(), writer, req_id))
                elif kind == Msg.STATUS:
                    if loop.time() >= self._muted_until:      # silent while muted
                        writer.write(encode(Msg.STATUS_REPLY, req_id, self.status()))
                elif kind == Msg.STATS:
                    writer.write(encode(Msg.STATS_REPLY, req_id, json.dumps(self.stats()).encode()))
                elif kind == Msg.RESET:
                    self.max_queue = self.queue_length
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
            arrived, writer, req_id = await self._jobs.get()
            # Service starts when the job arrives or when the previous job
            # finishes, whichever is later. Sleeping until that absolute
            # deadline, instead of for a duration, means a late timer wake-up
            # delays only this reply and never slows the server's true
            # service rate, which stays exactly 1 / mean_service.
            start = max(arrived, finish)
            service = self._rng.expovariate(1.0 / self.mean_service)
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
            if not writer.is_closing():
                writer.write(encode(Msg.RESPONSE, req_id, self._reply))

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
        }


async def serve(server_id: int, host: str, port: int, mean_service: float,
                seed: int | None) -> None:
    backend = BackendServer(server_id, mean_service, seed)
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
    args = ap.parse_args(argv)
    try:
        run(serve(args.id, args.host, args.port, args.mean_service, args.seed))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
