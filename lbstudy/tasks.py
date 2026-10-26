"""Task classes: fast, moderate and slow requests, timed on this computer.

Every request does the same real work, compressing a block of text with zlib
(as a web server does when it gzips a response). Its class sets how big the
block is, like a supermarket shopper with a few items, a basket or a full
cart. Each request is fast, moderate or slow at random, in fixed shares
(``config.TASK_CLASSES``).

``python -m lbstudy.tasks`` runs every task on its own, one at a time and in
random order, many times, and writes the measured times to
results/tasks.json. The testbed servers then replay those measurements: a
request takes as long as one randomly picked measurement of its class.
Replaying instead of compressing for real keeps the servers independent of
each other. Several CPU-bound servers on one laptop would compete for the
same cores, and the experiment would measure the laptop instead of the load
balancer.

    python -m lbstudy.tasks              # measure and write results/tasks.json
    python -m lbstudy.tasks --show       # print the summary of an existing file
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

from . import config
from .platform_tuning import tune_process

DEFAULT_PATH = Path("results/tasks.json")
CLASS_NAMES = tuple(name for name, _, _ in config.TASK_CLASSES)
_WORDS = ("load balancer server queue request stale fresh report choice random robin "
          "least power two time age cpu memory task node cashier shopper cart basket").split()


def payload(kb: int, seed: int = 0) -> bytes:
    """Deterministic English-like text, so it compresses like a real page (~6:1)."""
    rng = random.Random(seed)
    size = kb * 1024
    words = []
    total = 0
    while total < size:
        word = rng.choice(_WORDS)
        words.append(word)
        total += len(word) + 1
    return " ".join(words).encode()[:size]


def compress(data: bytes) -> int:
    return len(zlib.compress(data, 6))


@dataclass(frozen=True)
class Workload:
    """Measured task times, as the servers and the load generator use them."""
    names: tuple[str, ...]
    shares: tuple[float, ...]
    times_s: tuple[tuple[float, ...], ...]

    @property
    def mean_service(self) -> float:
        """Mean processing time of a request, in seconds (sets each server's capacity)."""
        return sum(share * statistics.fmean(times)
                   for share, times in zip(self.shares, self.times_s))


def load(path: str | Path = DEFAULT_PATH) -> Workload:
    data = json.loads(Path(path).read_text())
    classes = data["classes"]
    return Workload(names=tuple(c["name"] for c in classes),
                    shares=tuple(c["share"] for c in classes),
                    times_s=tuple(tuple(t / 1000 for t in c["times_ms"]) for c in classes))


def measure(samples: int, seed: int = 1) -> dict:
    tune_process()
    blocks = {name: payload(kb) for name, _, kb in config.TASK_CLASSES}
    for data in blocks.values():                 # warm up caches and the allocator
        for _ in range(3):
            compress(data)
    order = [name for name in CLASS_NAMES for _ in range(samples)]
    random.Random(seed).shuffle(order)           # machine drift hits every class alike
    times = {name: [] for name in CLASS_NAMES}
    for name in order:
        began = time.perf_counter()
        compress(blocks[name])
        times[name].append((time.perf_counter() - began) * 1000)

    classes = []
    for name, share, kb in config.TASK_CLASSES:
        t = sorted(times[name])
        classes.append({
            "name": name, "share": share, "payload_kb": kb,
            "mean_ms": round(statistics.fmean(t), 3),
            "p50_ms": round(t[len(t) // 2], 3),
            "p95_ms": round(t[min(len(t) - 1, int(0.95 * len(t)))], 3),
            "times_ms": [round(x, 3) for x in times[name]],
        })
    return {
        "task": "zlib.compress(level 6) of English-like text; the class sets the size",
        "samples_per_class": samples,
        "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": platform.python_version(),
        "processor": platform.processor(),
        "mean_service_ms": round(sum(c["share"] * c["mean_ms"] for c in classes), 3),
        "classes": classes,
    }


def summary(data: dict) -> str:
    lines = [f"{'class':<10}{'share':>7}{'payload':>10}{'mean':>10}{'median':>10}{'p95':>10}",
             "-" * 57]
    for c in data["classes"]:
        lines.append(f"{c['name']:<10}{c['share']:>7.0%}{c['payload_kb']:>7} KB"
                     f"{c['mean_ms']:>7.1f} ms{c['p50_ms']:>7.1f} ms{c['p95_ms']:>7.1f} ms")
    mean = data["mean_service_ms"]
    lines.append(f"\naverage request: {mean:.1f} ms, so one server handles about "
                 f"{1000 / mean:.0f} requests/s")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(DEFAULT_PATH))
    ap.add_argument("--samples", type=int, default=200, help="measurements per class")
    ap.add_argument("--show", action="store_true", help="only print an existing file")
    args = ap.parse_args(argv)
    out = Path(args.out)
    if args.show:
        print(summary(json.loads(out.read_text())))
        return
    print(f"timing {args.samples} runs of each task, one at a time "
          f"(about {args.samples * 0.15:.0f} s; keep other apps quiet) ...", flush=True)
    data = measure(args.samples)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1))
    print(summary(data))
    print(f"\nwritten to {out}")


if __name__ == "__main__":
    main()
