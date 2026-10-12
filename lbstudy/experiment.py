"""Run one experiment: start the testbed, drive it with load, collect metrics.

Every run starts fresh processes: five backend servers and one load balancer
(four in the shared-snapshot scenario), each a separate Python process
listening on its own TCP port. No queue or connection state therefore
carries over from one run to the next. The load generator runs inside this
(the runner's) process, which also fires the scenario events.

Random seeds depend only on the traffic level and repetition (plus the
strategy, for the balancer's own random choices), never on the staleness or
scenario. Every strategy therefore faces exactly the same arrival times, each
server draws the same sequence of processing times, and a randomized strategy
makes the same random draws at every staleness level (common random numbers).
The differences between configurations are then not blurred by differences
in luck.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from . import config, loadgen, metrics
from .platform_tuning import run
from .protocol import Link, Msg

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def derive_seed(*parts) -> int:
    """Stable seed from any tuple of values (Python's hash() is randomized)."""
    return int.from_bytes(hashlib.sha256(repr(parts).encode()).digest()[:6], "big")


@dataclass(frozen=True)
class RunConfig:
    strategy: str
    staleness: float
    load: float
    rep: int = 0
    scenario: str = "staleness"
    n_servers: int = config.N_SERVERS
    mean_service: float = config.MEAN_SERVICE_S
    warmup: float = config.WARMUP_S
    duration: float = config.DURATION_S
    timeout: float = config.TIMEOUT_S
    a1: float = config.FALB_A1
    a2: float = config.FALB_A2
    host: str = field(default=config.HOST, compare=False)

    @classmethod
    def for_scenario(cls, strategy: str, scenario: str, rep: int = 0, **kw) -> "RunConfig":
        sc = config.SCENARIOS[scenario]
        return cls(strategy=strategy, staleness=sc["staleness"], load=sc["load"], rep=rep,
                   scenario=scenario, **kw)

    @property
    def spec(self) -> dict:
        return config.SCENARIOS.get(self.scenario, {})

    @property
    def balancers(self) -> int:
        return self.spec.get("balancers", 1)

    @property
    def arrival_rate(self) -> float:
        return self.load * self.n_servers / self.mean_service

    @property
    def run_id(self) -> str:
        prefix = "" if self.scenario == "staleness" else f"{self.scenario}_"
        return f"{prefix}{self.strategy}_T{self.staleness:g}_L{self.load:g}_r{self.rep}"

    def seed(self, purpose: str, *extra) -> int:
        if purpose == "strategy":
            return derive_seed(purpose, self.strategy, self.load, self.rep, *extra)
        return derive_seed(purpose, self.load, self.rep, *extra)


class Testbed:
    """Context manager that runs the servers and balancers as child processes."""

    def __init__(self, cfg: RunConfig, log_file):
        self.cfg = cfg
        self._log = log_file
        self._procs: list[subprocess.Popen] = []
        self.server_ports: list[int] = []
        self.lb_ports: list[int] = []

    def _spawn(self, module: str, *args) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-m", module, *map(str, args)],
            cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=self._log, text=True)
        self._procs.append(proc)
        return proc

    def __enter__(self) -> "Testbed":
        cfg = self.cfg
        try:
            servers = [self._spawn("lbstudy.server", "--id", i, "--host", cfg.host,
                                   "--port", 0, "--mean-service", cfg.mean_service,
                                   "--seed", cfg.seed("service", i))
                       for i in range(cfg.n_servers)]
            self.server_ports = [_wait_ready(p) for p in servers]
            backends = ",".join(f"{cfg.host}:{p}" for p in self.server_ports)
            shared = ["--aligned-polls"] if cfg.balancers > 1 else []
            balancers = [self._spawn("lbstudy.balancer", "--backends", backends,
                                     "--strategy", cfg.strategy, "--staleness", cfg.staleness,
                                     "--a1", cfg.a1, "--a2", cfg.a2, *shared,
                                     "--host", cfg.host, "--port", 0,
                                     "--seed", cfg.seed("strategy", b))
                         for b in range(cfg.balancers)]
            self.lb_ports = [_wait_ready(p) for p in balancers]
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        for proc in self._procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in self._procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if proc.stdout:
                proc.stdout.close()
        self._procs.clear()


def _wait_ready(proc: subprocess.Popen, timeout: float = 30.0) -> int:
    """Read the child's "READY <port>" line (without blocking forever)."""
    line: list[str] = []
    reader = threading.Thread(target=lambda: line.append(proc.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    text = line[0].strip() if line else ""
    if not text.startswith("READY "):
        raise RuntimeError(f"{proc.args[2]} did not start (exit code {proc.poll()}, "
                           f"output {text!r}); see the run log for its error")
    return int(text.split()[1])


def scenario_event(cfg: RunConfig) -> dict | None:
    """The CONTROL message for this run's scenario event, if it has one."""
    spec = cfg.spec
    if spec.get("event") == "mute":
        return {"mute": spec["duration"]}
    if spec.get("event") == "slow":
        return {"slow": spec["factor"], "duration": spec["duration"]}
    return None


async def _drive(cfg: RunConfig, testbed: Testbed):
    servers = [await Link.open(cfg.host, port) for port in testbed.server_ports]
    balancers = [await Link.open(cfg.host, port) for port in testbed.lb_ports]
    controls = servers + balancers
    event = scenario_event(cfg)

    async def start_window() -> None:
        opened = asyncio.get_running_loop().time()
        await asyncio.gather(*(c.call(Msg.RESET) for c in controls))
        if event is not None:
            await asyncio.sleep(opened + config.EVENT_START_S - asyncio.get_running_loop().time())
            await servers[config.EVENT_SERVER].call(Msg.CONTROL, json.dumps(event).encode())

    result = await loadgen.generate(
        cfg.host, testbed.lb_ports, rate=cfg.arrival_rate, duration=cfg.duration,
        warmup=cfg.warmup, timeout=cfg.timeout, seed=cfg.seed("arrivals"),
        on_window_start=start_window)

    replies = await asyncio.gather(*(c.call(Msg.STATS) for c in controls))
    stats = [json.loads(payload) for _, payload in replies]
    for c in controls:
        await c.close()
    return result, stats[:len(servers)], stats[len(servers):]


def _mean_of(stats: list[dict], key: str):
    values = [s[key] for s in stats if s.get(key) is not None]
    return float(np.mean(values)) if values else None


def summarize(cfg: RunConfig, result: loadgen.LoadResult, server_stats: list[dict],
              lb_stats: list[dict]) -> dict:
    measured = result.measured
    lat = metrics.latency_summary(result.latency[measured], cfg.timeout)
    lag_ms = result.send_lag[measured] * 1000
    dispatched = np.sum([s["dispatched"] for s in lb_stats], axis=0).tolist()
    max_queues = [s["max_queue"] for s in server_stats]
    row = {
        "scenario": cfg.scenario, "strategy": cfg.strategy, "staleness": cfg.staleness,
        "load": cfg.load, "rep": cfg.rep, "n_servers": cfg.n_servers,
        "balancers": cfg.balancers, "mean_service_ms": 1000 * cfg.mean_service,
        "warmup_s": cfg.warmup, "duration_s": cfg.duration, "timeout_s": cfg.timeout,
        "falb_a1": cfg.a1, "falb_a2": cfg.a2,
        "target_rps": cfg.arrival_rate,
        "offered_rps": lat["n_requests"] / cfg.duration,
        "throughput_rps": lat["n_ok"] / cfg.duration,
        **lat,
        "event_mean_ms": None, "event_p99_ms": None,
        "max_queue": max(max_queues),
        "max_queue_per_server": "|".join(map(str, max_queues)),
        "dispatch_cov": metrics.coefficient_of_variation(dispatched),
        "dispatched_per_server": "|".join(map(str, dispatched)),
        "decision_us": _mean_of(lb_stats, "decision_us"),
        "decision_max_us": max(s["decision_max_us"] for s in lb_stats),
        "lb_polls": int(np.mean([s["polls"] for s in lb_stats])),
        "lb_missed_probes": sum(s["missed_probes"] for s in lb_stats),
        "lb_probe_rtt_ms": _mean_of(lb_stats, "probe_rtt_ms"),
        "mode_all": _mean_of(lb_stats, "mode_all"),
        "mode_two": _mean_of(lb_stats, "mode_two"),
        "mode_random": _mean_of(lb_stats, "mode_random"),
        "gen_lag_mean_ms": float(lag_ms.mean()) if lag_ms.size else float("nan"),
        "gen_lag_p99_ms": float(np.percentile(lag_ms, 99)) if lag_ms.size else float("nan"),
    }
    if scenario_event(cfg) is not None:
        start = config.EVENT_START_S
        during = measured & (result.scheduled >= start) & (result.scheduled < start + cfg.spec["duration"])
        ev = metrics.latency_summary(result.latency[during], cfg.timeout)
        row.update(event_mean_ms=ev["mean_ms"], event_p99_ms=ev["p99_ms"])
    return row


def save_raw(path: Path, cfg: RunConfig, result: loadgen.LoadResult,
             server_stats: list[dict], summary: dict) -> None:
    """Per-request data and queue traces, for histograms and time-series plots."""
    traces = {}
    for s in server_stats:
        trace = np.asarray(s["trace"], dtype=float).reshape(-1, 2)
        traces[f"trace_t{s['server_id']}"] = trace[:, 0] - result.window_start_unix
        traces[f"trace_q{s['server_id']}"] = trace[:, 1].astype(np.int32)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path, scheduled=result.scheduled, latency=result.latency, server=result.server,
        send_lag=result.send_lag, summary=json.dumps(summary),
        config=json.dumps(asdict(cfg)), **traces)


def run_once(cfg: RunConfig, raw_dir: Path | None = None, log_dir: Path | None = None) -> dict:
    """Run one configuration end to end and return its summary row."""
    started = time.time()
    with tempfile.TemporaryFile(mode="w+") as log:
        try:
            with Testbed(cfg, log) as testbed:
                result, server_stats, lb_stats = run(_drive(cfg, testbed))
        except BaseException:
            _keep_log(log, log_dir, cfg)
            raise
        _keep_log(log, log_dir, cfg)
    summary = summarize(cfg, result, server_stats, lb_stats)
    summary["wall_time_s"] = round(time.time() - started, 1)
    summary["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if raw_dir is not None:
        save_raw(Path(raw_dir) / f"{cfg.run_id}.npz", cfg, result, server_stats, summary)
    return summary


def _keep_log(log, log_dir: Path | None, cfg: RunConfig) -> None:
    """Keep the child processes' stderr only when they actually wrote something."""
    log.flush()
    log.seek(0)
    text = log.read()
    if text.strip():
        if log_dir is not None:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            (Path(log_dir) / f"{cfg.run_id}.log").write_text(text)
        else:
            print(text, file=sys.stderr)
