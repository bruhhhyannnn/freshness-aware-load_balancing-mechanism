"""Run the full factorial experiment on the real testbed, unattended.

Default plan, from the case study (5 strategies, 3 repetitions each):
  staleness scenario:  6 poll intervals x 3 traffic levels      = 270 runs
  missing_report, slow_server, shared_snapshot: 1 setting each  =  45 runs
Each run is a 10 s warm-up plus a 60 s measurement window: about 6.5 hours.

* Results are appended to ``runs.csv`` after every run, so an interrupted
  sweep resumes where it stopped when started again with the same options.
* Within each repetition the run order is shuffled, so slow drifts on the
  machine (temperature, background jobs) do not line up with any one factor.
* On Windows the machine is kept awake until the sweep finishes.

Examples:
    python -m lbstudy.sweep --dry-run          # show the plan and time estimate
    python -m lbstudy.sweep                    # the full experiment
    python -m lbstudy.sweep --quick            # ~45 min smoke test of the whole pipeline
    python -m lbstudy.sweep --scenarios slow_server missing_report   # a subset
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import platform
import random
import sys
import time
import traceback
from pathlib import Path

from . import config
from .experiment import RunConfig, run_once
from .platform_tuning import keep_awake, tune_process

KEY_FIELDS = ("scenario", "strategy", "staleness", "load", "rep")


def window(scenario: str, duration: float) -> float:
    """Measurement window: long enough to contain the scenario's event + 10 s."""
    spec = config.SCENARIOS.get(scenario, {})
    if "event" in spec:
        return max(duration, config.EVENT_START_S + spec["duration"] + 10.0)
    return duration


def plan(args) -> list[RunConfig]:
    common = dict(warmup=args.warmup, timeout=args.timeout)
    runs = []
    for rep in range(args.reps):
        batch = []
        if "staleness" in args.scenarios:
            batch += [RunConfig(strategy=s, staleness=T, load=l, rep=rep,
                                duration=args.duration, **common)
                      for s, T, l in itertools.product(args.strategies, args.staleness, args.loads)]
        batch += [RunConfig.for_scenario(s, name, rep, duration=window(name, args.duration),
                                         **common)
                  for name in args.scenarios if name != "staleness" for s in args.strategies]
        if not args.no_shuffle:
            random.Random(rep).shuffle(batch)
        runs += batch
    return runs


def run_key(scenario, strategy, staleness, load, rep) -> tuple:
    return (str(scenario), str(strategy), float(staleness), float(load), int(rep))


def completed_runs(csv_path: Path, args) -> set[tuple]:
    if not csv_path.exists():
        return set()
    done = set()
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            settings = (float(row["warmup_s"]), float(row["duration_s"]))
            expected = (args.warmup, window(row["scenario"], args.duration))
            if settings != expected:
                sys.exit(f"{csv_path} holds runs with warm-up/duration {settings}, but this "
                         f"sweep uses {expected}. Use another --out folder.")
            done.add(run_key(*(row[k] for k in KEY_FIELDS)))
    return done


def append_row(csv_path: Path, row: dict) -> None:
    new_file = not csv_path.exists()
    with open(csv_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def fmt_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    return f"{h}h{rem // 60:02d}m" if h else f"{rem // 60}m{rem % 60:02d}s"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="results/testbed", help="output folder")
    ap.add_argument("--scenarios", nargs="+", default=["staleness", *config.SCENARIOS],
                    choices=["staleness", *config.SCENARIOS])
    ap.add_argument("--strategies", nargs="+", default=list(config.STRATEGIES))
    ap.add_argument("--staleness", nargs="+", type=float, default=list(config.STALENESS_S))
    ap.add_argument("--loads", nargs="+", type=float, default=list(config.LOADS))
    ap.add_argument("--reps", type=int, default=config.REPETITIONS)
    ap.add_argument("--warmup", type=float, default=config.WARMUP_S)
    ap.add_argument("--duration", type=float, default=config.DURATION_S)
    ap.add_argument("--timeout", type=float, default=config.TIMEOUT_S)
    ap.add_argument("--quick", action="store_true",
                    help="5 s warm-up, 15 s runs, 1 repetition (writes to results/testbed_quick)")
    ap.add_argument("--no-shuffle", action="store_true")
    ap.add_argument("--no-raw", action="store_true", help="skip per-request .npz files")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    args = ap.parse_args(argv)
    if args.quick:
        args.warmup, args.duration, args.reps = 5.0, 15.0, 1
        if args.out == "results/testbed":
            args.out = "results/testbed_quick"

    out = Path(args.out)
    csv_path = out / "runs.csv"
    runs = plan(args)
    done = completed_runs(csv_path, args)
    todo = [r for r in runs
            if run_key(r.scenario, r.strategy, r.staleness, r.load, r.rep) not in done]
    estimate = sum(r.warmup + r.duration + 4 for r in todo)
    print(f"{len(runs)} runs planned, {len(runs) - len(todo)} already done, {len(todo)} to go "
          f"(~{fmt_duration(estimate)}; badly herding runs take longer)")
    if args.dry_run:
        for r in todo[:12]:
            print("  ", r.run_id)
        print("   ..." if len(todo) > 12 else "")
        return
    if not todo:
        print(f"nothing to do; results are in {csv_path}")
        return

    out.mkdir(parents=True, exist_ok=True)
    (out / "sweep.json").write_text(json.dumps({
        "scenarios": {name: config.SCENARIOS.get(name, "grid") for name in args.scenarios},
        "event_server": config.EVENT_SERVER, "event_start_s": config.EVENT_START_S,
        "falb_a1": config.FALB_A1, "falb_a2": config.FALB_A2,
        "strategies": args.strategies, "staleness_s": args.staleness, "loads": args.loads,
        "reps": args.reps, "warmup_s": args.warmup, "duration_s": args.duration,
        "timeout_s": args.timeout, "n_servers": config.N_SERVERS,
        "mean_service_ms": 1000 * config.MEAN_SERVICE_S,
        "python": sys.version, "platform": platform.platform(),
        "processor": platform.processor(), "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, indent=2))

    tune_process()
    keep_awake(True)
    started = time.time()
    failures = 0
    try:
        for k, cfg in enumerate(todo, 1):
            print(f"[{k}/{len(todo)}] {cfg.run_id} ... ", end="", flush=True)
            try:
                row = run_once(cfg, raw_dir=None if args.no_raw else out / "raw",
                               log_dir=out / "logs")
            except KeyboardInterrupt:
                raise
            except Exception:
                failures += 1
                (out / "logs").mkdir(parents=True, exist_ok=True)
                with open(out / "logs" / "failures.log", "a") as f:
                    f.write(f"--- {cfg.run_id} {time.ctime()}\n{traceback.format_exc()}\n")
                print("FAILED (see logs/failures.log; rerun the sweep to retry)")
                continue
            append_row(csv_path, row)
            elapsed = time.time() - started
            eta = elapsed / k * (len(todo) - k)
            print(f"p99 {row['p99_ms']:.0f} ms, mean {row['mean_ms']:.0f} ms, "
                  f"max queue {row['max_queue']}, timeouts {row['n_timeout']}  "
                  f"[elapsed {fmt_duration(elapsed)}, ETA {fmt_duration(eta)}]", flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted; run the same command again to resume")
    finally:
        keep_awake(False)
    print(f"done: {len(todo) - failures} runs written to {csv_path}"
          + (f", {failures} failed" if failures else ""))


if __name__ == "__main__":
    main()
