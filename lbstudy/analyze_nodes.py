"""Figures and tables for the follow-up experiment (``python -m lbstudy.sweep --plan nodes``).

Answers the three follow-up questions:
  * fast / moderate / slow tasks: how long does each kind of request take?
  * per server: how busy (CPU utilization) and how idle is each server, how
    many tasks does it handle, and how much memory does it use?
  * more servers: what happens from 1 server up to what this laptop can run?

Reads results/testbed_nodes/ (runs.csv, raw/*.npz, tasks.json) and writes
figures and report.md to results/analysis_nodes/.

    python -m lbstudy.analyze_nodes
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.ticker import FuncFormatter

from . import config
from .analyze import (INK, INK_2, MUTED, SURFACE, header, log_axis, mean_sd, plot_vs_staleness,
                      save, setup_style, strategies_in, strategy_handles, style_of)
from .strategies import label

BUSY_CMAP = LinearSegmentedColormap.from_list("busy", ["#f7f6f2", "#c3c2b7", "#52514e", "#0b0b0b"])
COLS = ["p99_ms", "mean_ms", "p95_ms", "n_timeout", "busy_pct_mean", "busy_pct_min",
        "busy_pct_max", "idle_pct_mean", "idle_while_waiting_pct", "tasks_cov",
        "peak_mem_mb_max", "os_cpu_pct_mean", "decision_us", "gen_lag_p99_ms",
        "offered_rps", "target_rps", "throughput_rps"]


def aggregate(df: pd.DataFrame, keys: list[str], cols: list[str]) -> pd.DataFrame:
    cols = [c for c in cols if c in df.columns]
    agg = df.groupby(keys)[cols].agg(["mean", "std", "min", "max", "count"])
    agg.columns = [f"{m}_{s}" for m, s in agg.columns]
    return agg.reset_index()


def per_server_mean(runs: pd.DataFrame, column: str) -> list[float]:
    """Element-wise mean over repetitions of a "a|b|c" per-server column."""
    rows = [list(map(float, str(v).split("|"))) for v in runs[column] if isinstance(v, str) and v]
    return list(np.mean(rows, axis=0)) if rows else []


def fmt(v, digits: int = 0) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return f"{v:,.{digits}f}"


# --- figures ------------------------------------------------------------------

def plot_by_class(grid_agg: pd.DataFrame, workload: dict, strategies: list[str], T: float,
                  path: Path) -> None:
    classes = [c["name"] for c in workload["classes"]]
    sub = grid_agg[np.isclose(grid_agg.staleness, T)]
    fig, ax = plt.subplots(figsize=(9.6, 4.9))
    width = 0.8 / len(strategies)
    x = np.arange(len(classes))
    for k, s in enumerate(strategies):
        row = sub[sub.strategy == s]
        if row.empty:
            continue
        row = row.iloc[0]
        mean = np.array([row[f"{c}_p99_ms_mean"] for c in classes])
        lo = mean - np.array([row[f"{c}_p99_ms_min"] for c in classes])
        hi = np.array([row[f"{c}_p99_ms_max"] for c in classes]) - mean
        color, _ = style_of(s)
        ax.bar(x - 0.4 + width * (k + 0.5), mean, width * 0.9, color=color, zorder=3,
               yerr=[lo, hi] if row[f"{classes[0]}_p99_ms_count"] > 1 else None,
               error_kw=dict(ecolor=INK_2, elinewidth=0.9, capsize=2))
    for i, c in enumerate(workload["classes"]):         # the task on its own, no waiting
        alone = float(np.percentile(c["times_ms"], 99))
        ax.hlines(alone, i - 0.42, i + 0.42, color=INK, lw=1.6, zorder=4)
        ax.annotate(f"task alone {alone:.0f} ms", (i + 0.42, alone), xytext=(0, 3),
                    textcoords="offset points", ha="right", va="bottom", fontsize=8.5, color=INK)
    ax.set_xticks(x, [f"{c['name']}\n({c['share']:.0%} of requests, ~{c['mean_ms']:.0f} ms)"
                      for c in workload["classes"]])
    ax.grid(False, axis="x")
    ax.set_ylabel("p99 response time (ms, log scale)")
    log_axis(ax)
    header(fig, "Response time by task class",
           f"5 servers, 90% load, T = {T:g} s. Bar = mean p99 of the repetitions, whisker = "
           "min–max. Black line = the task's own processing time; the rest is waiting in a queue.",
           strategy_handles(strategies), top=0.78)
    save(fig, path)


def plot_heatmap(raw_dir: Path, strategies: list[str], T: float, load: float,
                 path: Path) -> bool:
    maps = []
    for s in strategies:
        f = raw_dir / f"classes_{s}_T{T:g}_L{load:g}_r0.npz"
        if f.exists():
            maps.append((s, np.load(f)["util_per_s"]))
    if not maps:
        return False
    fig, axes = plt.subplots(len(maps), 1, figsize=(10, 0.95 * len(maps) + 1.9), sharex=True,
                             squeeze=False)
    im = None
    for ax, (s, util) in zip(axes[:, 0], maps):
        n, secs = util.shape
        im = ax.imshow(util, aspect="auto", cmap=BUSY_CMAP, vmin=0, vmax=100,
                       interpolation="nearest", extent=[0, secs, n - 0.5, -0.5])
        ax.set_yticks(range(n), [f"S{i + 1}" for i in range(n)], fontsize=8)
        ax.set_ylabel(f"{label(s)}\nidle {100 - util.mean():.0f}%", rotation=0, ha="right",
                      va="center", color=style_of(s)[0], fontsize=10, weight="semibold")
        ax.grid(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_visible(False)
    axes[-1, 0].set_xlabel("Seconds into the measurement window")
    header(fig, "CPU utilization of every server, second by second",
           f"5 servers, 90% load, T = {T:g} s, first repetition. Dark = busy processing; "
           "light = idle. Light patches while other rows are dark are wasted capacity.",
           None, top=1 - 0.8 / (0.95 * len(maps) + 1.9))
    fig.subplots_adjust(right=0.9)      # room for the colour bar, set before it is placed
    cax = fig.add_axes([0.925, axes[-1, 0].get_position().y0, 0.015,
                        axes[0, 0].get_position().y1 - axes[-1, 0].get_position().y0])
    cbar = fig.colorbar(im, cax=cax)
    cbar.set_label("busy (% of each second)")
    cbar.outline.set_visible(False)
    save(fig, path)
    return True


def plot_scaling(scale_agg: pd.DataFrame, strategies: list[str], timeout_ms: float,
                 path: Path) -> None:
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11.4, 4.9))
    counts = sorted(scale_agg.n_servers.unique())
    for s in reversed(strategies):
        d = scale_agg[scale_agg.strategy == s].sort_values("n_servers")
        if d.empty:
            continue
        color, marker = style_of(s)
        for ax, m in ((a1, "p99_ms"), (a2, "idle_while_waiting_pct")):
            if (d[f"{m}_count"] > 1).any():
                ax.fill_between(d.n_servers, d[f"{m}_min"], d[f"{m}_max"], color=color,
                                alpha=0.12, lw=0, zorder=2)
            ax.plot(d.n_servers, d[f"{m}_mean"], color=color, marker=marker, ms=7, mec=SURFACE,
                    mew=1.2, lw=2.6 if s == "falb" else 2, zorder=4 if s == "falb" else 3)
    a1.axhline(timeout_ms, color=MUTED, lw=1, ls=":")
    a1.annotate(f"timeout ({timeout_ms / 1000:g} s): slower requests count as this",
                (counts[0], timeout_ms), xytext=(2, -3), textcoords="offset points", va="top",
                fontsize=8.5, color=MUTED)
    log_axis(a1)
    a1.set_ylabel("p99 response time (ms, log scale)")
    a1.set_title("Slowest 1% of requests")
    a2.set_ylabel("idle while others wait (% of time)")
    a2.set_ylim(bottom=0)
    a2.set_title("Capacity wasted")
    for ax in (a1, a2):
        ax.set_xscale("log")
        ax.set_xticks(counts, [f"{c:g}" for c in counts])
        ax.xaxis.set_minor_formatter(FuncFormatter(lambda *_: ""))
        ax.grid(False, which="minor")
        ax.set_xlabel("Number of servers (log scale)")
    header(fig, "From 1 server to what this laptop can run",
           f"90% load, T = {config.NODES_SCALE_T:g} s, fast/moderate/slow tasks. Line = mean of "
           "the repetitions; band = min–max.", strategy_handles(strategies), top=0.76)
    save(fig, path)


# --- tables -------------------------------------------------------------------

def workload_table(workload: dict) -> list[str]:
    out = ["| Class | share of requests | payload | measured mean | median | p95 |",
           "|---|---:|---:|---:|---:|---:|"]
    for c in workload["classes"]:
        out.append(f"| {c['name']} | {c['share']:.0%} | {c['payload_kb']:,} KB | "
                   f"{c['mean_ms']:.1f} ms | {c['p50_ms']:.1f} ms | {c['p95_ms']:.1f} ms |")
    mean = workload["mean_service_ms"]
    return out + ["", f"Average request: {mean:.1f} ms, so one server handles about "
                  f"{1000 / mean:.0f} requests/s. Each task compresses a block of text with zlib; "
                  f"timed {workload['samples_per_class']} times per class on "
                  f"{workload['measured_at']}.", ""]


def metric_by(agg: pd.DataFrame, strategies: list[str], key: str, metric: str,
              key_label: str, digits: int = 0) -> list[str]:
    levels = sorted(agg[key].unique())
    out = [f"| {key_label} | " + " | ".join(label(s) for s in strategies) + " |",
           "|---:|" + "---:|" * len(strategies)]
    for v in levels:
        cells = []
        for s in strategies:
            row = agg[(agg.strategy == s) & np.isclose(agg[key], v)]
            cells.append(mean_sd(row.iloc[0], metric, digits) if not row.empty else "n/a")
        out.append(f"| {v:g} | " + " | ".join(cells) + " |")
    return out + [""]


def class_table(grid_agg, workload, strategies, T) -> list[str]:
    classes = [c["name"] for c in workload["classes"]]
    sub = grid_agg[np.isclose(grid_agg.staleness, T)]
    out = ["| Strategy | " + " | ".join(f"{c} p99 (ms)" for c in classes) + " |",
           "|---|" + "---:|" * len(classes)]
    out.append("| *task alone* | " + " | ".join(
        f"*{np.percentile(c['times_ms'], 99):,.0f}*" for c in workload["classes"]) + " |")
    for s in strategies:
        row = sub[sub.strategy == s]
        if not row.empty:
            out.append(f"| {label(s)} | " + " | ".join(
                mean_sd(row.iloc[0], f"{c}_p99_ms") for c in classes) + " |")
    return out + [""]


def server_table(runs: pd.DataFrame, strategies: list[str], T: float) -> list[str]:
    sub = runs[np.isclose(runs.staleness, T)]
    out = ["| Strategy | busy % per server (CPU utilization) | idle % | idle while others wait % "
           "| tasks per server | peak memory per server (MB) | OS CPU % per server |",
           "|---|---|---:|---:|---|---|---|"]
    for s in strategies:
        g = sub[sub.strategy == s]
        if g.empty:
            continue

        def joined(col, digits=0):
            return " / ".join(f"{v:,.{digits}f}" for v in per_server_mean(g, col))
        out.append(f"| {label(s)} | {joined('busy_pct_per_server')} | "
                   f"{g.idle_pct_mean.mean():.1f} | {g.idle_while_waiting_pct.mean():.1f} | "
                   f"{joined('tasks_per_server')} | {joined('peak_mem_mb_per_server', 1)} | "
                   f"{joined('os_cpu_pct_per_server', 1)} |")
    return out + [""]


def health_table(scale: pd.DataFrame) -> list[str]:
    out = ["| Servers | target rate (req/s) | achieved | generator lag p99, worst run (ms) "
           "| FALB decision time (µs) | timed-out requests (all strategies) |",
           "|---:|---:|---:|---:|---:|---:|"]
    for n, g in scale.groupby("n_servers"):
        falb = g[g.strategy == "falb"].decision_us.mean()
        out.append(f"| {n} | {g.target_rps.mean():,.0f} | "
                   f"{100 * (g.offered_rps / g.target_rps).mean():.1f}% | "
                   f"{g.gen_lag_p99_ms.max():.2f} | {fmt(falb, 1)} | {int(g.n_timeout.sum()):,} |")
    return out + [""]


def best_line(agg: pd.DataFrame, key: str, v: float, strategies: list[str]) -> str:
    sub = agg[np.isclose(agg[key], v)].set_index("strategy")
    if sub.empty:
        return ""
    best = sub.p99_ms_mean.idxmin()
    falb = sub.p99_ms_mean.get("falb", math.nan)
    rr = sub.p99_ms_mean.get("round_robin", math.nan)
    return (f"Lowest p99: **{label(best)}** ({sub.p99_ms_mean[best]:,.0f} ms). "
            f"FALB {falb:,.0f} ms, Round Robin {rr:,.0f} ms.")


# --- main ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--testbed", default="results/testbed_nodes")
    ap.add_argument("--out", default="results/analysis_nodes")
    args = ap.parse_args(argv)
    tb, out = Path(args.testbed), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    setup_style()

    df = pd.read_csv(tb / "runs.csv")
    df = df[df.workload == "classes"]
    workload = json.loads((tb / "tasks.json").read_text())
    classes = [c["name"] for c in workload["classes"]]
    cols = COLS + [f"{c}_p99_ms" for c in classes]
    strategies = strategies_in(df)
    T = config.NODES_SCALE_T
    load = float(df.load.iloc[0])
    grid = df[df.n_servers == config.N_SERVERS]
    scale = df[np.isclose(df.staleness, T)]
    grid_agg = aggregate(grid, ["strategy", "staleness", "load"], cols)
    scale_agg = aggregate(scale, ["strategy", "n_servers"], cols)
    source = (f"Real asyncio testbed, {config.N_SERVERS} servers, {load:.0%} load, fast/moderate/"
              "slow tasks. ")

    plot_vs_staleness(grid_agg, "p99_ms", "99th-percentile response time vs. age of load "
                      "information", "p99 response time (ms, log scale)",
                      out / "p99_vs_staleness.png", source=source, strategies=strategies)
    plot_vs_staleness(grid_agg, "idle_while_waiting_pct", "Capacity wasted: servers idle while "
                      "requests wait at another server", "idle while others wait (% of time)",
                      out / "idle_vs_staleness.png", log=False, source=source,
                      strategies=strategies)
    plot_by_class(grid_agg, workload, strategies, T, out / "p99_by_class.png")
    has_heatmap = plot_heatmap(tb / "raw", strategies, T, load, out / "utilization_heatmap.png")
    plot_scaling(scale_agg, strategies, 1000 * float(df.timeout_s.iloc[0]), out / "scaling.png")

    lines = ["# Follow-up: task classes, per-server utilization and more servers", "",
             f"Testbed data: `{tb / 'runs.csv'}` ({len(df)} runs). All figures are in this folder.",
             "", "## Workload: fast, moderate and slow tasks", ""]
    lines += workload_table(workload)
    lines += ["## Response time vs. age of load information (5 servers)", "",
              "![p99 vs staleness](p99_vs_staleness.png)", "",
              f"p99 response time, ms, mean ± sd ({load:.0%} load):", ""]
    lines += metric_by(grid_agg, strategies, "staleness", "p99_ms", "T (s)")
    lines += ["## Response time by task class", "", "![by class](p99_by_class.png)", "",
              best_line(grid_agg, "staleness", T, strategies), ""]
    lines += class_table(grid_agg, workload, strategies, T)
    lines += ["## Per-server utilization, idle time, tasks and memory", ""]
    if has_heatmap:
        lines += ["![utilization heatmap](utilization_heatmap.png)", ""]
    lines += [f"5 servers, T = {T:g} s, mean over repetitions. Values per server are listed "
              "S1 / S2 / ... *Busy %* is the CPU utilization of the server's worker (the share "
              "of time it was processing). *Idle while others wait* is the share of time a "
              "server sat idle while a request was queued at another server: capacity the "
              "load balancer wasted. *OS CPU %* is the process's real CPU time, i.e. the "
              "request-handling overhead, since task times are replayed.", ""]
    lines += server_table(grid, strategies, T)
    lines += ["![idle vs staleness](idle_vs_staleness.png)", "",
              "Idle while others wait, % of time, mean ± sd:", ""]
    lines += metric_by(grid_agg, strategies, "staleness", "idle_while_waiting_pct", "T (s)", 1)
    lines += [f"## More servers (T = {T:g} s)", "", "![scaling](scaling.png)", "",
              "p99 response time, ms, mean ± sd:", ""]
    lines += metric_by(scale_agg, strategies, "n_servers", "p99_ms", "Servers")
    lines += ["Idle while others wait, % of time:", ""]
    lines += metric_by(scale_agg, strategies, "n_servers", "idle_while_waiting_pct", "Servers", 1)
    lines += ["## Testbed health by number of servers", "",
              "Whether the laptop kept up: the load generator should send on schedule "
              "(lag of a few ms) and every request should be sent.", ""]
    lines += health_table(scale)
    lines += ["## Totals", "",
              f"- Runs: {len(df)}; requests measured: {int(df.n_requests.sum()):,}; "
              f"timed out: {int(df.n_timeout.sum()):,} "
              f"({', '.join(f'{label(s)} {int(n):,}' for s, n in df.groupby('strategy').n_timeout.sum().items() if n)})."
              if df.n_timeout.sum() else
              f"- Runs: {len(df)}; requests measured: {int(df.n_requests.sum()):,}; none timed out.",
              f"- Achieved vs. target arrival rate: "
              f"{100 * (df.offered_rps / df.target_rps).mean():.1f}% on average.", ""]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out / 'report.md'} and {len(list(out.glob('*.png')))} figures")


if __name__ == "__main__":
    main()
