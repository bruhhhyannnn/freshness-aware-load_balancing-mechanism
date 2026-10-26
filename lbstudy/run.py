"""Run one or a few configurations on the real testbed and print the results.

Examples:
    python -m lbstudy.run --strategy falb --staleness 1 --load 0.9
    python -m lbstudy.run --strategy all --staleness 2 --load 0.9 --duration 20
    python -m lbstudy.run --strategy all --scenario slow_server
"""

from __future__ import annotations

import argparse

from . import config, tasks
from .experiment import RunConfig, run_once
from .strategies import label

COLUMNS = [("mean_ms", "mean"), ("p95_ms", "p95"), ("p99_ms", "p99"), ("event_p99_ms", "evt p99"),
           ("max_queue", "maxQ"), ("dispatch_cov", "CoV"), ("throughput_rps", "thr/s"),
           ("n_timeout", "timeouts"), ("decision_us", "dec us")]


def _fmt(key: str, value) -> str:
    if value is None or value != value:     # None or NaN
        return f"{'-':>9}"
    if key == "dispatch_cov":
        return f"{value:>9.3f}"
    return f"{value:>9.1f}" if isinstance(value, float) else f"{value:>9}"


def print_rows(rows: list[dict]) -> None:
    head = f"{'strategy':<22}{'T (s)':>6}{'load':>6}" + "".join(f"{h:>9}" for _, h in COLUMNS)
    print(head)
    print("-" * len(head))
    for r in rows:
        print(f"{label(r['strategy']):<22}{r['staleness']:>6g}{r['load']:>6g}"
              + "".join(_fmt(k, r.get(k)) for k, _ in COLUMNS))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--strategy", default="falb",
                    help="round_robin | random | least_loaded | p2c | falb | falb_cpu | ... | all")
    ap.add_argument("--scenario", default="staleness", choices=["staleness", *config.SCENARIOS],
                    help="a named scenario sets its own load and poll interval")
    ap.add_argument("--staleness", type=float, default=1.0, help="poll interval T in seconds")
    ap.add_argument("--load", type=float, default=0.9, help="fraction of total capacity")
    ap.add_argument("--duration", type=float, default=None,
                    help="measurement window (default 20 s; 60 s for scenarios)")
    ap.add_argument("--warmup", type=float, default=5.0)
    ap.add_argument("--timeout", type=float, default=config.TIMEOUT_S)
    ap.add_argument("--a1", type=float, default=config.FALB_A1)
    ap.add_argument("--a2", type=float, default=config.FALB_A2)
    ap.add_argument("--rep", type=int, default=0, help="repetition number (selects the seeds)")
    ap.add_argument("--servers", type=int, default=config.N_SERVERS, help="number of servers")
    ap.add_argument("--workload", default="exponential", choices=["exponential", "classes"],
                    help="classes = fast/moderate/slow tasks from --tasks-file")
    ap.add_argument("--tasks-file", default=str(tasks.DEFAULT_PATH))
    ap.add_argument("--raw-dir", default=None, help="also save per-request data here")
    args = ap.parse_args(argv)

    names = config.STRATEGIES if args.strategy == "all" else [args.strategy]
    common = dict(warmup=args.warmup, timeout=args.timeout, a1=args.a1, a2=args.a2, rep=args.rep,
                  n_servers=args.servers, workload=args.workload)
    if args.workload == "classes":
        common.update(tasks_file=args.tasks_file,
                      mean_service=tasks.load(args.tasks_file).mean_service)
    rows = []
    for name in names:
        if args.scenario == "staleness":
            cfg = RunConfig(strategy=name, staleness=args.staleness, load=args.load,
                            duration=args.duration or 20.0, **common)
        else:
            cfg = RunConfig.for_scenario(name, args.scenario,
                                         duration=args.duration or config.DURATION_S, **common)
        print(f"running {cfg.run_id}: {cfg.arrival_rate:.0f} req/s for "
              f"{cfg.warmup:g}+{cfg.duration:g} s ...", flush=True)
        rows.append(run_once(cfg, raw_dir=args.raw_dir))
    print()
    print_rows(rows)


if __name__ == "__main__":
    main()
