"""Turn experiment results into tables, figures and hypothesis verdicts.

Reads whatever exists of:
    results/testbed/runs.csv            (python -m lbstudy.sweep)
    results/testbed/raw/*.npz           (per-request data, queue traces)
    results/simulation/validate.csv     (python -m lbstudy.simulate validate)
    results/simulation/ablation.csv     (python -m lbstudy.simulate extend)
    results/simulation/pools.csv        (    "                          )
    results/simulation/balancers.csv    (    "                          )
and writes figures, summary tables and ``report.md`` to results/analysis/.

Examples:
    python -m lbstudy.analyze
    python -m lbstudy.analyze --testbed results/testbed_quick --out results/analysis_quick
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter  # noqa: E402

from . import config, metrics  # noqa: E402
from .strategies import label  # noqa: E402

# --- visual style ---------------------------------------------------------
# Colour follows the strategy in every figure (validated categorical palette,
# fixed slot order; FALB, the proposed mechanism, takes slot 1). Every
# strategy also has its own marker shape, so the figures survive grayscale
# printing and colour-vision deficiency.
SURFACE = "#ffffff"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
EVENT_WASH = "#f0efec"
STYLE = {                      # strategy: (colour, marker)
    "falb": ("#2a78d6", "o"),
    "least_loaded": ("#eb6834", "s"),
    "round_robin": ("#1baf7a", "^"),
    "random": ("#eda100", "D"),
    "p2c": ("#e87ba4", "v"),
    "falb_cpu": ("#008300", "P"),
    "falb_nocorr": ("#4a3aa7", "X"),
    "falb_noadapt": ("#e34948", "h"),
    # The palette has eight slots; this FALB variant shares FALB's hue family
    # (a darker blue step) and is told apart by its dashed line and marker.
    "falb_turns": ("#184f95", "d"),
}
DASHED = {"falb_turns"}
ORDER = ["falb", "p2c", "least_loaded", "round_robin", "random",
         "falb_turns", "falb_noadapt", "falb_nocorr", "falb_cpu"]
BASELINES = ["round_robin", "random", "least_loaded", "p2c"]
METRICS = ["mean_ms", "p95_ms", "p99_ms", "p999_ms", "max_queue", "dispatch_cov",
           "throughput_rps", "n_timeout", "event_mean_ms", "event_p99_ms", "decision_us"]
SCENARIO_TITLES = {
    "missing_report": "Missing report: one server silent for {duration:g} s",
    "slow_server": "Slow server: one server {factor:g}x slower for {duration:g} s",
    "shared_snapshot": "{balancers} balancers sharing one snapshot",
}


def setup_style() -> None:
    plt.rcParams.update({
        "font.family": ["Segoe UI", "Arial", "DejaVu Sans"],
        "font.size": 10,
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": AXIS, "axes.linewidth": 0.8,
        "axes.labelcolor": INK_2, "axes.titlecolor": INK, "axes.titlesize": 11,
        "axes.titleweight": "semibold", "axes.titlelocation": "left",
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "grid.linestyle": "-",
        "xtick.color": MUTED, "ytick.color": MUTED,
        "xtick.labelcolor": INK_2, "ytick.labelcolor": INK_2,
        "legend.frameon": False, "legend.fontsize": 10,
        "lines.solid_capstyle": "round", "lines.solid_joinstyle": "round",
    })


def strategies_in(df: pd.DataFrame) -> list[str]:
    present = set(df["strategy"])
    return [s for s in ORDER if s in present] + sorted(present - set(ORDER))


def style_of(strategy: str) -> tuple[str, str]:
    return STYLE.get(strategy, (INK_2, "x"))


def staleness_label(T: float) -> str:
    return "0\nfresh" if T == 0 else f"{T:g}"


def thousands(v, _pos=None) -> str:
    return f"{v:,.0f}" if v >= 1 else f"{v:g}"


def log_axis(ax, axis: str = "y", decades_only: bool = False) -> None:
    getattr(ax, f"set_{axis}scale")("log")
    target = ax.yaxis if axis == "y" else ax.xaxis
    target.set_major_locator(LogLocator(base=10, subs=(1.0,) if decades_only else (1.0, 2.0, 5.0)))
    target.set_major_formatter(FuncFormatter(thousands))
    target.set_minor_formatter(NullFormatter())
    ax.grid(False, which="minor", axis=axis)


def header(fig, title: str, subtitle: str, handles, top: float = 0.80) -> None:
    fig.text(0.012, 0.975, title, ha="left", va="top", fontsize=13, weight="semibold", color=INK)
    fig.text(0.012, 0.93, subtitle, ha="left", va="top", fontsize=9.5, color=INK_2)
    if handles:
        fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.006, 0.895),
                   ncol=min(len(handles), 6), handlelength=2.4, columnspacing=1.4)
    fig.subplots_adjust(top=top)


def strategy_handles(strategies) -> list[Line2D]:
    return [Line2D([], [], color=style_of(s)[0], marker=style_of(s)[1], lw=2, ms=7,
                   ls="--" if s in DASHED else "-", mec=SURFACE, mew=1.2, label=label(s))
            for s in strategies]


def save(fig, path: Path) -> None:
    fig.savefig(path, dpi=200, bbox_inches="tight")
    plt.close(fig)


# --- aggregation ------------------------------------------------------------

def aggregate(df: pd.DataFrame, keys=("strategy", "staleness", "load")) -> pd.DataFrame:
    cols = [m for m in METRICS if m in df.columns]
    agg = df.groupby(list(keys))[cols].agg(["mean", "std", "min", "max", "count"])
    agg.columns = [f"{m}_{stat}" for m, stat in agg.columns]
    return agg.reset_index()


def cell(agg: pd.DataFrame, strategy: str, T: float, load: float):
    row = agg[(agg.strategy == strategy) & np.isclose(agg.staleness, T) & np.isclose(agg.load, load)]
    return None if row.empty else row.iloc[0]


def fmt(v, digits: int = 0) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return f"{v:,.{digits}f}"


def mean_sd(row, metric: str, digits: int = 0) -> str:
    if row is None:
        return "n/a"
    mean, sd, n = row[f"{metric}_mean"], row[f"{metric}_std"], row[f"{metric}_count"]
    return (f"{fmt(mean, digits)} ± {fmt(sd, digits)}" if n > 1 and not math.isnan(sd)
            else fmt(mean, digits))


def clearly_lower(a, b, metric: str = "p99_ms") -> bool:
    """True if every repetition of ``a`` beat every repetition of ``b`` (needs >= 2 each)."""
    return (min(a[f"{metric}_count"], b[f"{metric}_count"]) >= 2
            and a[f"{metric}_max"] < b[f"{metric}_min"])


def sure(a, b, metric: str = "p99_ms") -> str:
    if min(a[f"{metric}_count"], b[f"{metric}_count"]) < 2:
        return "single run"
    return "clear" if clearly_lower(a, b, metric) or clearly_lower(b, a, metric) else "within noise"


# --- raw per-request data -------------------------------------------------------

def raw_runs(raw_dir: Path, stem: str) -> list:
    return [np.load(f) for f in sorted(raw_dir.glob(f"{stem}_r*.npz"))]


def run_stem(strategy: str, T: float, load: float, scenario: str = "staleness") -> str:
    prefix = "" if scenario == "staleness" else f"{scenario}_"
    return f"{prefix}{strategy}_T{T:g}_L{load:g}"


def measured_latency_ms(runs) -> tuple[np.ndarray, np.ndarray]:
    """Latency (ms; no reply counts as the timeout) and send time of measured requests."""
    lats, sched = [], []
    for d in runs:
        cfg = json.loads(str(d["config"]))
        m = (d["scheduled"] >= 0) & (d["scheduled"] < cfg["duration"])
        lats.append(np.fmin(np.nan_to_num(d["latency"][m], nan=np.inf), cfg["timeout"]) * 1000)
        sched.append(d["scheduled"][m])
    if not lats:
        return np.array([]), np.array([])
    return np.concatenate(lats), np.concatenate(sched)


# --- figures: staleness scenario ----------------------------------------------

def plot_vs_staleness(agg: pd.DataFrame, metric: str, title: str, ylabel: str, path: Path,
                      *, log: bool = True, theory=None, source: str = "",
                      strategies: list[str] | None = None) -> None:
    """One panel per load level; ``theory(load)`` draws an M/M/1 reference line."""
    loads = sorted(agg.load.unique())
    levels = sorted(agg.staleness.unique())
    pos = {T: i for i, T in enumerate(levels)}
    strategies = strategies or strategies_in(agg)
    fig, axes = plt.subplots(1, len(loads), figsize=(4.3 * len(loads) + 0.4, 4.9),
                             sharey=log, squeeze=False)
    for ax, load in zip(axes[0], loads):
        sub = agg[np.isclose(agg.load, load)]
        for s in reversed(strategies):                 # FALB drawn last, on top
            d = sub[sub.strategy == s].sort_values("staleness")
            if d.empty or d[f"{metric}_mean"].isna().all():
                continue
            x = [pos[T] for T in d.staleness]
            color, marker = style_of(s)
            if (d[f"{metric}_count"] > 1).any():
                ax.fill_between(x, d[f"{metric}_min"], d[f"{metric}_max"], color=color,
                                alpha=0.12, lw=0, zorder=2)
            ax.plot(x, d[f"{metric}_mean"], color=color, marker=marker, lw=2.6 if s == "falb" else 2,
                    ls="--" if s in DASHED else "-", ms=7, mec=SURFACE, mew=1.2,
                    zorder=4 if s == "falb" else 3)
        if theory is not None:
            ref = theory(load)
            ax.axhline(ref, color=MUTED, lw=1, zorder=1)
            ax.annotate("M/M/1 theory = Random", (len(levels) - 1, ref), xytext=(0, 3),
                        textcoords="offset points", ha="right", va="bottom", fontsize=8.5,
                        color=MUTED)
        ax.set_title(f"{load:.0%} of capacity")
        ax.set_xticks(range(len(levels)), [staleness_label(T) for T in levels])
        ax.set_xlim(-0.3, len(levels) - 0.7)
        ax.grid(False, axis="x")
        ax.set_xlabel("Load information age: poll interval T (s)")
        if log:
            log_axis(ax)
        else:
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
            ax.set_ylim(bottom=0)
    axes[0][0].set_ylabel(ylabel)
    reps = int(agg[f"{metric}_count"].max())
    band = "; band = min–max across repetitions" if reps > 1 else ""
    header(fig, title, f"{source}Line = mean of {reps} repetition{'s' * (reps > 1)}{band}.",
           strategy_handles(strategies), top=0.76)
    save(fig, path)


def plot_histograms(raw_dir: Path, conditions: list[tuple[str, str, float, float]],
                    strategies: list[str], title: str, subtitle: str, path: Path) -> bool:
    """Response-time distributions: one row per strategy, one column per condition.

    ``conditions`` holds (column title, scenario, T, load). A narrow shape far
    to the left means fast and consistent responses.
    """
    data = {(s, c): measured_latency_ms(raw_runs(raw_dir, run_stem(s, T, load, sc)))[0]
            for s in strategies for c, (_, sc, T, load) in enumerate(conditions)}
    if not any(v.size for v in data.values()):
        return False
    # Exponential job times put ~14 % of requests under 3 ms, so start at 0.5 ms.
    bins = np.logspace(np.log10(0.5), np.log10(config.TIMEOUT_S * 1000 * 1.05), 80)
    fig, axes = plt.subplots(len(strategies), len(conditions), sharex=True, sharey=True,
                             figsize=(4.0 * len(conditions) + 1.2, 1.25 * len(strategies) + 1.9),
                             squeeze=False)
    for r, s in enumerate(strategies):
        color = style_of(s)[0]
        for c, (col_title, *_rest) in enumerate(conditions):
            ax = axes[r][c]
            lat = data[(s, c)]
            ax.grid(False, axis="x")
            if lat.size:
                share, _ = np.histogram(lat, bins=bins)
                share = share / lat.size
                ax.fill_between(bins[:-1], share, step="post", color=color, alpha=0.22, lw=0)
                ax.step(bins[:-1], share, where="post", color=color, lw=1.4)
                p50, p99 = np.percentile(lat, [50, 99])
                for v in (p50, p99):
                    ax.axvline(v, color=INK_2, lw=0.9, ymax=0.85)
                ax.text(0.015, 0.92, f"median {p50:,.0f} ms\np99 {p99:,.0f} ms",
                        transform=ax.transAxes, ha="left", va="top", fontsize=8, color=INK_2,
                        linespacing=1.3)
            if r == 0:
                ax.set_title(col_title, fontsize=10)
            if c == 0:
                ax.set_ylabel(label(s).replace(" (proposed)", "\n(proposed)"), rotation=0,
                              ha="right", va="center", fontsize=9)
            ax.set_yticks([])
            ax.spines["left"].set_visible(False)
    for ax in axes[-1]:
        log_axis(ax, "x", decades_only=True)
        ax.set_xlabel("Response time (ms, log scale)")
    axes[0][0].set_xlim(bins[0], bins[-1])
    header(fig, title, subtitle, None, top=1 - 1.05 / (1.25 * len(strategies) + 1.9))
    save(fig, path)
    return True


def plot_queue_traces(raw_dir: Path, stems: dict[str, str], title: str, subtitle: str,
                      path: Path, window: tuple[float, float] = (0.0, 30.0),
                      event: tuple[float, float] | None = None, highlight: int | None = None) -> bool:
    shown = [s for s, stem in stems.items() if (raw_dir / f"{stem}_r0.npz").exists()]
    if len(shown) < 2:
        return False
    data = {s: np.load(raw_dir / f"{stems[s]}_r0.npz") for s in shown}
    n = len([k for k in data[shown[0]].files if k.startswith("trace_q")])
    fig, axes = plt.subplots(n, len(shown), figsize=(4.0 * len(shown) + 0.6, 1.1 * n + 2.0),
                             sharex=True, sharey=True, squeeze=False)
    for col, s in enumerate(shown):
        color = style_of(s)[0]
        for i in range(n):
            ax = axes[i][col]
            if event is not None:
                ax.axvspan(*event, color=EVENT_WASH, lw=0, zorder=0)
            t, q = data[s][f"trace_t{i}"], data[s][f"trace_q{i}"]
            keep = (t >= window[0]) & (t <= window[1])
            ax.fill_between(t[keep], q[keep], step="post", color=color, alpha=0.12, lw=0)
            ax.step(t[keep], q[keep], where="post", color=color, lw=1.3)
            ax.grid(False, axis="x")
            if col == 0:
                name = f"Server {i + 1}" + (" ←" if i == highlight else "")
                ax.set_ylabel(name, rotation=0, ha="right", va="center")
        axes[0][col].set_title(label(s))
        axes[-1][col].set_xlabel("Time in measurement window (s)")
    axes[0][0].set_xlim(*window)
    for ax in axes.flat:
        ax.set_ylim(bottom=0)
    header(fig, title, subtitle, strategy_handles(shown), top=1 - 1.3 / (1.1 * n + 2.0))
    save(fig, path)
    return True


def plot_latency_ccdf(raw_dir: Path, load: float, levels: list[float], strategies: list[str],
                      path: Path) -> bool:
    panels = [T for T in levels if any(raw_dir.glob(f"*_T{T:g}_L{load:g}_r*.npz"))]
    if not panels:
        return False
    fig, axes = plt.subplots(1, len(panels), figsize=(4.3 * len(panels) + 0.4, 4.6),
                             sharey=True, squeeze=False)
    floor = 1e-4
    for ax, T in zip(axes[0], panels):
        for s in reversed(strategies):
            lat, _ = measured_latency_ms(raw_runs(raw_dir, run_stem(s, T, load)))
            if not lat.size:
                continue
            x = np.sort(lat)
            y = 1.0 - np.arange(x.size) / x.size
            keep = y >= floor
            color, marker = style_of(s)
            ax.plot(x[keep], y[keep], color=color, lw=2.4 if s == "falb" else 1.8)
            ax.plot(x[keep][-1:], y[keep][-1:], color=color, marker=marker, ms=7, mec=SURFACE,
                    mew=1.2, ls="none")
        ax.axhline(0.01, color=MUTED, lw=1, zorder=1)
        ax.annotate("p99", (1, 0.01), xycoords=("axes fraction", "data"), xytext=(-2, 3),
                    textcoords="offset points", ha="right", fontsize=8.5, color=MUTED)
        log_axis(ax, "x")
        ax.set_yscale("log")
        ax.set_ylim(floor, 1.05)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        ax.set_title("Fresh information (T = 0)" if T == 0 else f"T = {T:g} s")
        ax.set_xlabel("Response time (ms)")
    axes[0][0].set_ylabel("Fraction of requests slower than x")
    header(fig, "How slow is the slowest traffic?",
           f"Complementary CDF of response time at {load:.0%} load, all repetitions pooled. "
           "Lower and further left is better.", strategy_handles(strategies), top=0.76)
    save(fig, path)
    return True


# --- figures: event scenarios -----------------------------------------------------

def scenario_title(name: str) -> str:
    return SCENARIO_TITLES.get(name, name).format(**config.SCENARIOS.get(name, {}))


def plot_scenario_timeline(raw_dir: Path, scenarios: list[str], strategies: list[str],
                           path: Path, bin_s: float = 2.0) -> bool:
    panels = [sc for sc in scenarios if "event" in config.SCENARIOS[sc]]
    panels = [sc for sc in panels if any(raw_dir.glob(f"{sc}_*.npz"))]
    if not panels:
        return False
    fig, axes = plt.subplots(1, len(panels), figsize=(5.6 * len(panels) + 0.4, 4.8),
                             sharey=True, squeeze=False)
    for ax, sc in zip(axes[0], panels):
        spec = config.SCENARIOS[sc]
        start, end = config.EVENT_START_S, config.EVENT_START_S + spec["duration"]
        ax.axvspan(start, end, color=EVENT_WASH, lw=0, zorder=0)
        ax.annotate("event", ((start + end) / 2, 0), xycoords=("data", "axes fraction"),
                    xytext=(0, 4), textcoords="offset points", ha="center", fontsize=8.5,
                    color=MUTED)
        for s in reversed(strategies):
            lat, sched = measured_latency_ms(raw_runs(raw_dir, run_stem(s, spec["staleness"],
                                                                          spec["load"], sc)))
            if not lat.size:
                continue
            edges = np.arange(0, sched.max() + bin_s, bin_s)
            idx = np.digitize(sched, edges) - 1
            x, y = [], []
            for b in range(len(edges) - 1):
                in_bin = lat[idx == b]
                if in_bin.size >= 20:
                    x.append(edges[b] + bin_s / 2)
                    y.append(np.percentile(in_bin, 99))
            color, marker = style_of(s)
            ax.plot(x, y, color=color, lw=2.4 if s == "falb" else 1.8, zorder=4 if s == "falb" else 3)
            ax.plot(x[-1:], y[-1:], color=color, marker=marker, ms=7, mec=SURFACE, mew=1.2, ls="none")
        log_axis(ax)
        ax.grid(False, axis="x")
        ax.set_title(scenario_title(sc))
        ax.set_xlabel("Time in measurement window (s)")
    axes[0][0].set_ylabel(f"p99 response time per {bin_s:g} s (ms)")
    header(fig, "Response time before, during and after the event",
           f"99th percentile of requests sent in each {bin_s:g} s slice, repetitions pooled. "
           "Shaded: the event.", strategy_handles(strategies), top=0.76)
    save(fig, path)
    return True


def plot_scenario_summary(df: pd.DataFrame, scenarios: list[str], path: Path) -> bool:
    panels = [sc for sc in scenarios if (df.scenario == sc).any()]
    if not panels:
        return False
    fig, axes = plt.subplots(1, len(panels), figsize=(4.4 * len(panels) + 1.2, 3.9), sharey=True,
                             squeeze=False)
    strategies = strategies_in(df[df.scenario.isin(panels)])
    ys = {s: len(strategies) - 1 - k for k, s in enumerate(strategies)}
    for ax, sc in zip(axes[0], panels):
        metric = "event_p99_ms" if "event" in config.SCENARIOS[sc] else "p99_ms"
        sub = df[df.scenario == sc]
        for s in strategies:
            vals = sub[sub.strategy == s][metric].dropna()
            if vals.empty:
                continue
            color, marker = style_of(s)
            ax.plot(vals, [ys[s]] * len(vals), ls="none", marker=marker, ms=5, color=color,
                    alpha=0.45, mec="none")
            ax.plot([vals.mean()], [ys[s]], ls="none", marker=marker, ms=10, color=color,
                    mec=SURFACE, mew=1.5, zorder=4)
            ax.annotate(f"{vals.mean():,.0f}", (vals.mean(), ys[s]), xytext=(9, 0),
                        textcoords="offset points", va="center", fontsize=8.5, color=INK_2)
        log_axis(ax, "x")
        ax.grid(False, axis="y")
        ax.set_title(scenario_title(sc), fontsize=10)
        ax.set_xlabel("p99 during the event (ms)" if metric == "event_p99_ms" else "p99 (ms)")
        ax.margins(x=0.25)
    axes[0][0].set_yticks(list(ys.values()), [label(s) for s in ys])
    header(fig, "Scenarios: 99th-percentile response time",
           "Large marker and number = mean; small markers = individual repetitions. Left is better.",
           None, top=0.80)
    save(fig, path)
    return True


# --- figures: simulation ------------------------------------------------------

def plot_sim_vs_testbed(tb: pd.DataFrame, sim: pd.DataFrame, path: Path) -> None:
    loads = sorted(set(tb.load) & set(sim.load))
    levels = sorted(set(tb.staleness) | set(sim.staleness))
    pos = {T: i for i, T in enumerate(levels)}
    strategies = strategies_in(tb)
    fig, axes = plt.subplots(1, len(loads), figsize=(4.3 * len(loads) + 0.4, 4.9),
                             sharey=True, squeeze=False)
    for ax, load in zip(axes[0], loads):
        for s in strategies:
            color, marker = style_of(s)
            d = sim[(sim.strategy == s) & np.isclose(sim.load, load)].sort_values("staleness")
            ax.plot([pos[T] for T in d.staleness], d.p99_ms_mean, color=color, lw=2, alpha=0.9)
            d = tb[(tb.strategy == s) & np.isclose(tb.load, load)].sort_values("staleness")
            x = np.array([pos[T] for T in d.staleness])
            ax.vlines(x, d.p99_ms_min, d.p99_ms_max, color=color, lw=1.2)
            ax.plot(x, d.p99_ms_mean, ls="none", marker=marker, color=color, ms=7,
                    mec=SURFACE, mew=1.2, zorder=4)
        ax.set_title(f"{load:.0%} of capacity")
        ax.set_xticks(range(len(levels)), [staleness_label(T) for T in levels])
        ax.grid(False, axis="x")
        ax.set_xlabel("Load information age: poll interval T (s)")
        log_axis(ax)
    axes[0][0].set_ylabel("99th-percentile response time (ms)")
    handles = strategy_handles(strategies) + [
        Line2D([], [], color=INK_2, lw=2, label="Simulation"),
        Line2D([], [], color=INK_2, marker="o", ls="none", ms=7, mec=SURFACE, label="Testbed")]
    header(fig, "Testbed versus discrete-event simulation (n = 5 servers)",
           f"Lines: simulation, mean of {int(sim.p99_ms_count.max())} windows as long as the "
           "testbed's. Markers: real testbed mean, vertical bar = min–max across repetitions.",
           handles, top=0.72)
    save(fig, path)


def plot_panels_vs_staleness(agg: pd.DataFrame, panel_key: str, panel_title, title: str,
                             subtitle: str, path: Path) -> None:
    """p99 vs staleness, one panel per value of ``panel_key`` (load, pool size, ...)."""
    panels = sorted(agg[panel_key].unique())
    levels = sorted(agg.staleness.unique())
    pos = {T: i for i, T in enumerate(levels)}
    strategies = strategies_in(agg)
    fig, axes = plt.subplots(1, len(panels), figsize=(4.5 * len(panels) + 0.4, 4.9),
                             sharey=True, squeeze=False)
    for ax, value in zip(axes[0], panels):
        sub = agg[agg[panel_key] == value]
        for s in reversed(strategies):
            d = sub[sub.strategy == s].sort_values("staleness")
            color, marker = style_of(s)
            ax.plot([pos[T] for T in d.staleness], d.p99_ms_mean, color=color, marker=marker,
                    lw=2.6 if s == "falb" else 2, ms=6.5, mec=SURFACE, mew=1.1,
                    zorder=4 if s == "falb" else 3)
        ax.set_title(panel_title(value))
        log_axis(ax)
        ax.grid(False, axis="x")
        ax.set_xticks(range(len(levels)), [staleness_label(T) for T in levels], fontsize=8.5)
        ax.set_xlabel("Poll interval T (s)")
    axes[0][0].set_ylabel("p99 response time (ms)")
    header(fig, title, subtitle, strategy_handles(strategies), top=0.72)
    save(fig, path)


# --- report sections ----------------------------------------------------------------

def verdict_section(agg: pd.DataFrame, runs: pd.DataFrame) -> list[str]:
    loads = sorted(agg.load.unique())
    levels = sorted(agg.staleness.unique())
    out = ["## Hypotheses", "",
           "Verdicts compare the mean p99 over repetitions. A comparison is *clear* when every "
           "repetition of one strategy beat every repetition of the other, and *within noise* "
           "otherwise.", ""]

    def c(s, T, load):
        return cell(agg, s, T, load)

    # H1: FALB beats Least-Loaded once information is moderately or very stale.
    stale = [T for T in levels if T >= 0.5]
    out += ["### H1: FALB has a lower p99 than Least-Loaded once information is moderately "
            f"or very stale (T ≥ {min(stale, default=0.5):g} s)", ""]
    for load in loads:
        parts, held = [], 0
        for T in stale:
            f, ll = c("falb", T, load), c("least_loaded", T, load)
            if f is None or ll is None:
                continue
            ok = f["p99_ms_mean"] < ll["p99_ms_mean"]
            held += ok
            parts.append(f"T={T:g}: {fmt(f['p99_ms_mean'])} vs {fmt(ll['p99_ms_mean'])} ms "
                         f"(×{ll['p99_ms_mean'] / f['p99_ms_mean']:.1f}, {sure(f, ll)})")
        if parts:
            verdict = "**supported**" if held == len(parts) else f"**partly supported** ({held}/{len(parts)})"
            out.append(f"- {load:.0%} load: {verdict}. " + "; ".join(parts) + ".")
    out.append("")

    # H2: FALB matches or beats P2C when information is fresh.
    fresh = [T for T in levels if T < config.FALB_A1 + 1e-9 or T == 0]
    out += ["### H2: FALB matches or beats the Power of Two Choices when information is fresh "
            f"(T ≤ {config.FALB_A1:g} s)", "",
            "*Matches* means the two are within noise; *beats* means a lower mean p99.", ""]
    for load in loads:
        parts, held = [], 0
        for T in fresh:
            f, p = c("falb", T, load), c("p2c", T, load)
            if f is None or p is None:
                continue
            beats = f["p99_ms_mean"] < p["p99_ms_mean"]
            matches = not beats and sure(f, p) != "clear"
            held += beats or matches
            word = "beats" if beats else ("matches" if matches else "loses")
            parts.append(f"T={T:g}: FALB {mean_sd(f, 'p99_ms')} vs P2C {mean_sd(p, 'p99_ms')} ms "
                         f"({word}, {sure(f, p)})")
        if parts:
            verdict = "**supported**" if held == len(parts) else "**not supported**"
            out.append(f"- {load:.0%} load: {verdict}. " + "; ".join(parts) + ".")
    out.append("")

    # H3: scenarios.
    out += ["### H3: FALB keeps response times stable when a server stops reporting or slows "
            "down, while Round Robin and Random build long queues on the slow server", ""]
    for sc in ("missing_report", "slow_server"):
        sub = runs[runs.scenario == sc]
        if sub.empty:
            continue
        spec = config.SCENARIOS[sc]
        g = sub.groupby("strategy")
        ev = g.event_p99_ms.mean()
        normal = c("falb", spec["staleness"], spec["load"])
        evq = g.max_queue_per_server.apply(
            lambda col: np.mean([int(v.split("|")[config.EVENT_SERVER]) for v in col]))
        line = (f"- **{scenario_title(sc)}** (load {spec['load']:.0%}, T = {spec['staleness']:g} s). "
                f"p99 during the event: " + ", ".join(f"{label(s)} {fmt(ev[s])} ms"
                                                      for s in strategies_in(sub) if s in ev) + ". ")
        if normal is not None and "falb" in ev:
            line += (f"FALB's event p99 is ×{ev['falb'] / normal['p99_ms_mean']:.2f} its p99 with "
                     f"no event ({fmt(normal['p99_ms_mean'])} ms). ")
        line += ("Longest queue on the affected server: "
                 + ", ".join(f"{label(s)} {evq[s]:.0f}" for s in strategies_in(sub) if s in evq) + ".")
        best = min(ev.index, key=lambda s: ev[s])
        line += f" Best during the event: **{label(best)}**."
        out.append(line)
    out.append("")
    return out


def wins_section(agg: pd.DataFrame) -> list[str]:
    out = ["## FALB against the best baseline in every cell", "",
           "| Load | T (s) | FALB p99 (ms) | best baseline | its p99 (ms) | FALB vs. best baseline |",
           "|---:|---:|---:|---|---:|---:|"]
    wins = total = 0
    for load in sorted(agg.load.unique()):
        for T in sorted(agg.staleness.unique()):
            f = cell(agg, "falb", T, load)
            rows = {s: cell(agg, s, T, load) for s in BASELINES}
            rows = {s: r for s, r in rows.items() if r is not None}
            if f is None or not rows:
                continue
            best = min(rows, key=lambda s: rows[s]["p99_ms_mean"])
            b = rows[best]["p99_ms_mean"]
            change = (f["p99_ms_mean"] - b) / b
            total += 1
            wins += change < 0
            out.append(f"| {load:.0%} | {T:g} | {fmt(f['p99_ms_mean'])} | {label(best)} | {fmt(b)} | "
                       f"{'**' if change < 0 else ''}{change:+.0%}{'**' if change < 0 else ''} |")
    out.insert(2, f"FALB has the lowest p99 in **{wins} of {total}** staleness × load cells. "
                  "Negative = FALB is faster.\n")
    out.append("")
    return out


def p99_tables(agg: pd.DataFrame, metric: str = "p99_ms") -> list[str]:
    out = []
    strategies = strategies_in(agg)
    for load in sorted(agg.load.unique()):
        out += [f"**{load:.0%} of capacity** ({metric.replace('_ms', '')}, ms, mean ± sd)", "",
                "| T (s) | " + " | ".join(label(s) for s in strategies) + " |",
                "|---:|" + "---:|" * len(strategies)]
        for T in sorted(agg.staleness.unique()):
            out.append(f"| {T:g} | " + " | ".join(mean_sd(cell(agg, s, T, load), metric)
                                                 for s in strategies) + " |")
        out.append("")
    return out


def scenario_table(runs: pd.DataFrame) -> list[str]:
    out = []
    for sc in config.SCENARIOS:
        sub = runs[runs.scenario == sc]
        if sub.empty:
            continue
        spec = config.SCENARIOS[sc]
        agg = aggregate(sub, keys=("strategy",))
        event = "event" in spec
        out += [f"**{scenario_title(sc)}** (load {spec['load']:.0%}, T = {spec['staleness']:g} s, "
                f"{int(agg.p99_ms_count.max())} repetitions)", "",
                "| Strategy | mean (ms) | p99 (ms) | " + ("p99 during event (ms) | " if event else "")
                + "max queue | CoV |", "|---|---:|---:|" + ("---:|" if event else "") + "---:|---:|"]
        for s in strategies_in(sub):
            r = agg[agg.strategy == s].iloc[0]
            out.append(f"| {label(s)} | {mean_sd(r, 'mean_ms')} | {mean_sd(r, 'p99_ms')} | "
                       + (f"{mean_sd(r, 'event_p99_ms')} | " if event else "")
                       + f"{mean_sd(r, 'max_queue')} | {mean_sd(r, 'dispatch_cov', 3)} |")
        out.append("")
    return out


def decision_table(runs: pd.DataFrame) -> list[str]:
    if "decision_us" not in runs or runs.decision_us.isna().all():
        return []
    st = runs[runs.scenario == "staleness"]
    rows = []
    for s in strategies_in(st):
        sub = st[st.strategy == s]
        polled = sub[sub.staleness > 0].decision_us.mean()
        fresh = sub[sub.staleness == 0].decision_us.mean()
        rows.append(f"| {label(s)} | {fmt(polled, 1)} | {fmt(fresh, 1)} | "
                    f"{fmt(sub.decision_max_us.median(), 0)} |")
    return ["## Decision time", "",
            "Time the balancer spends choosing a server, measured around the strategy code only "
            "(with T = 0 the status probes themselves take a further "
            f"{fmt(st[st.staleness == 0].lb_probe_rtt_ms.mean(), 2)} ms round trip).", "",
            "| Strategy | mean, T > 0 (µs) | mean, T = 0 (µs) | median of per-run max (µs) |",
            "|---|---:|---:|---:|", *rows, ""]


def sanity_section(df: pd.DataFrame) -> list[str]:
    out = ["## Testbed sanity checks", ""]
    st = df[df.scenario == "staleness"]
    rnd = st[st.strategy == "random"]
    if not rnd.empty:
        out += ["Random assignment turns each server into an M/M/1 queue, so its measurements can "
                "be checked against theory. Theory uses the load actually offered in the window "
                "(Poisson arrivals vary from window to window). Runs of the same repetition share "
                "their random seeds, so only distinct repetitions are independent samples.", "",
                "| Target load | offered load | measured mean (ms) | M/M/1 mean | measured p99 (ms) "
                "| M/M/1 p99 | independent samples |",
                "|---:|---:|---:|---:|---:|---:|---:|"]
        for load, g in rnd.groupby("load"):
            rho = float((g.offered_rps / (g.n_servers / g.mean_service_ms * 1000)).mean())
            out.append(f"| {load:.0%} | {rho:.1%} | {g.mean_ms.mean():,.1f} | "
                       f"{metrics.mm1_mean_ms(rho, config.MEAN_SERVICE_S):,.1f} | "
                       f"{g.p99_ms.mean():,.0f} | "
                       f"{metrics.mm1_percentile_ms(rho, config.MEAN_SERVICE_S, 99):,.0f} | "
                       f"{g.rep.nunique()} |")
        out += ["", "A short window also under-reads the p99 on its own: a queue's worst "
                "excursions are rare, and a single minute often misses them. The simulator shows "
                "the size of this effect (see *Testbed vs. simulation*, which compares like with "
                "like: the same window length, repeated 20 times).", ""]
    out += [f"- Runs: {len(df)}; requests measured: {int(df.n_requests.sum()):,}; "
            f"timed out: {int(df.n_timeout.sum()):,}.",
            f"- Achieved vs. target arrival rate: {100 * (df.offered_rps / df.target_rps).mean():.1f}% "
            "on average.",
            f"- Load generator send lag (actual minus scheduled send time): mean "
            f"{df.gen_lag_mean_ms.mean():.2f} ms, worst run p99 {df.gen_lag_p99_ms.max():.2f} ms.", ""]
    return out


def simulation_sections(sim_dir: Path, out: Path, tb_agg: pd.DataFrame | None) -> list[str]:
    report = []
    validate = sim_dir / "validate.csv"
    if validate.exists():
        v = pd.read_csv(validate)
        vst = aggregate(v[v.scenario == "staleness"])
        vst.to_csv(out / "summary_sim_validate.csv", index=False)
        if tb_agg is not None:
            plot_sim_vs_testbed(tb_agg, vst, out / "sim_vs_testbed_p99.png")
            report += ["## Testbed vs. simulation", "", "![sim vs testbed](sim_vs_testbed_p99.png)", ""]
        report += ["## Scenarios in simulation (20 repetitions each)", "",
                   "The testbed runs each scenario 3 times; the simulator repeats them 20 times "
                   "to show the noise-free picture.", ""] + scenario_table(v)

    ablation = sim_dir / "ablation.csv"
    if ablation.exists():
        a = pd.read_csv(ablation)
        a = a[a.scenario == "staleness"]
        agg = aggregate(a)
        plot_vs_staleness(agg, "p99_ms", "Ablation: which part of FALB does the work?",
                          "p99 response time (ms, log scale)", out / "sim_ablation.png",
                          source="Simulation, 5 servers. ")
        report += ["## Ablation (simulation)", "", "![ablation](sim_ablation.png)", "",
                   "Each variant switches one FALB component off: *without correction* uses the raw "
                   "reports; *without adaptive choice* always compares all servers and never times "
                   "out; *CPU signal* replaces queue length with CPU utilization. *Turn-taking "
                   "fallback* is a design alternative: servers whose reports timed out take turns "
                   "instead of being picked at random.", ""]
        report += p99_tables(agg[np.isclose(agg.load, 0.9)])
    pools = sim_dir / "pools.csv"
    if pools.exists():
        agg = aggregate(pd.read_csv(pools), keys=("n_servers", "strategy", "staleness", "load"))
        plot_panels_vs_staleness(agg, "n_servers", lambda n: f"n = {n} servers, 90% load",
                                 "Simulation: larger server pools",
                                 "p99 response time vs. load information age.", out / "sim_pools.png")
        report += ["## Larger pools (simulation)", "", "![pools](sim_pools.png)", ""]
    balancers = sim_dir / "balancers.csv"
    if balancers.exists():
        agg = aggregate(pd.read_csv(balancers), keys=("balancers", "strategy", "staleness", "load"))
        plot_panels_vs_staleness(agg, "balancers",
                                 lambda b: f"{b} balancer{'s' if b > 1 else ''}, shared snapshot",
                                 "Simulation: several balancers sharing one snapshot",
                                 "90% load, 5 servers. Each balancer only knows its own requests.",
                                 out / "sim_balancers.png")
        report += ["## Several balancers (simulation)", "", "![balancers](sim_balancers.png)", "",
                   "With one balancer FALB's correction sees every request, and comparing all "
                   "servers is safe. With several balancers each correction sees only part of the "
                   "traffic; here the random two-choice step of FALB's adaptive choice limits the "
                   "herding that returns (compare FALB with *FALB without adaptive choice*).", ""]
        sub = agg[agg.strategy.isin(["falb", "falb_noadapt", "p2c"])]
        report += ["| balancers | T (s) | FALB | FALB without adaptive choice | P2C |",
                   "|---:|---:|---:|---:|---:|"]
        for (b, T), g in sub.groupby(["balancers", "staleness"]):
            vals = {s: g[g.strategy == s].p99_ms_mean for s in ("falb", "falb_noadapt", "p2c")}
            report.append(f"| {b} | {T:g} | " + " | ".join(
                fmt(v.iloc[0]) if len(v) else "n/a" for v in vals.values()) + " |")
        report.append("")
    return report


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--testbed", default="results/testbed")
    ap.add_argument("--simulation", default="results/simulation")
    ap.add_argument("--out", default="results/analysis")
    ap.add_argument("--trace-staleness", type=float, default=1.0,
                    help="poll interval used for the queue-trace (herding) figure")
    args = ap.parse_args(argv)

    setup_style()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    testbed, sim_dir = Path(args.testbed), Path(args.simulation)
    report = ["# Results: a freshness-aware load balancer (FALB) under stale load information", ""]
    agg = None

    runs_csv = testbed / "runs.csv"
    if runs_csv.exists():
        runs = pd.read_csv(runs_csv)
        if "scenario" not in runs:
            runs["scenario"] = "staleness"
        df = runs[runs.scenario == "staleness"]
        agg = aggregate(df)
        agg.to_csv(out / "summary_testbed.csv", index=False)
        raw = testbed / "raw"
        src = "Real asyncio testbed, 5 servers. "
        main_strats = strategies_in(df)
        plot_vs_staleness(agg, "p99_ms", "99th-percentile response time vs. age of load information",
                          "p99 response time (ms, log scale)", out / "p99_vs_staleness.png",
                          theory=lambda l: metrics.mm1_percentile_ms(l, config.MEAN_SERVICE_S, 99),
                          source=src)
        plot_vs_staleness(agg, "p95_ms", "95th-percentile response time vs. age of load information",
                          "p95 response time (ms, log scale)", out / "p95_vs_staleness.png",
                          theory=lambda l: metrics.mm1_percentile_ms(l, config.MEAN_SERVICE_S, 95),
                          source=src)
        plot_vs_staleness(agg, "mean_ms", "Mean response time vs. age of load information",
                          "Mean response time (ms, log scale)", out / "mean_vs_staleness.png",
                          theory=lambda l: metrics.mm1_mean_ms(l, config.MEAN_SERVICE_S), source=src)
        plot_vs_staleness(agg, "max_queue", "Longest queue reached by any server",
                          "Max queue length (requests, log scale)", out / "max_queue_vs_staleness.png",
                          source=src)
        plot_vs_staleness(agg, "dispatch_cov", "How evenly requests were spread",
                          "Coefficient of variation of requests per server", out / "cov_vs_staleness.png",
                          log=False, source=src)

        high = max(agg.load)
        figs = []
        hist_levels = [T for T in (0.0, 1.0, 5.0) if T in set(agg.staleness)]
        if plot_histograms(raw, [(("Fresh (T = 0)" if T == 0 else f"T = {T:g} s"), "staleness", T, high)
                                 for T in hist_levels], main_strats,
                           "Response-time distributions (the 'bell shape')",
                           f"{high:.0%} load, all repetitions pooled. Narrow and far left = fast and "
                           "consistent; vertical lines mark the median and p99.",
                           out / "histograms_staleness.png"):
            figs.append("histograms_staleness.png")
        T = args.trace_staleness
        if plot_queue_traces(raw, {s: run_stem(s, T, high) for s in ("least_loaded", "p2c", "falb")},
                             "Queue length at each server over time",
                             f"{high:.0%} load, polled every T = {T:g} s, repetition 1. Least-Loaded "
                             "sends a whole poll interval's traffic to one server at a time (herding).",
                             out / "queue_traces.png"):
            figs.append("queue_traces.png")
        if plot_latency_ccdf(raw, high, [T for T in (0.0, 1.0, 5.0) if T in set(agg.staleness)],
                             main_strats, out / "latency_ccdf.png"):
            figs.append("latency_ccdf.png")

        report += [f"Testbed data: `{runs_csv}` ({len(runs)} runs). All figures are in this folder.", "",
                   "![p99 vs staleness](p99_vs_staleness.png)", ""]
        report += verdict_section(agg, runs)
        report += wins_section(agg)
        report += ["## p99 response time (staleness scenario)", ""] + p99_tables(agg)

        scen = [sc for sc in config.SCENARIOS if (runs.scenario == sc).any()]
        if scen:
            report += ["## Scenarios", ""] + scenario_table(runs)
            if plot_scenario_summary(runs, scen, out / "scenario_summary.png"):
                report += ["![scenario summary](scenario_summary.png)", ""]
            if plot_scenario_timeline(raw, scen, main_strats, out / "scenario_timeline.png"):
                report += ["![scenario timeline](scenario_timeline.png)", ""]
            conds = [(scenario_title(sc), sc, config.SCENARIOS[sc]["staleness"],
                      config.SCENARIOS[sc]["load"]) for sc in scen]
            if plot_histograms(raw, conds, main_strats, "Response-time distributions in the scenarios",
                               "All repetitions pooled. Vertical lines mark the median and p99.",
                               out / "histograms_scenarios.png"):
                report += ["![scenario histograms](histograms_scenarios.png)", ""]
            if "slow_server" in scen:
                sc = config.SCENARIOS["slow_server"]
                ev = (config.EVENT_START_S, config.EVENT_START_S + sc["duration"])
                if plot_queue_traces(raw, {s: run_stem(s, sc["staleness"], sc["load"], "slow_server")
                                           for s in ("round_robin", "p2c", "falb")},
                                     "Queue length at each server: slow-server scenario",
                                     f"Server {config.EVENT_SERVER + 1} (arrow) is "
                                     f"{sc['factor']:g}x slower during the shaded period. "
                                     "Repetition 1.", out / "queue_traces_slow_server.png",
                                     window=(0.0, config.EVENT_START_S + sc["duration"] + 15),
                                     event=ev, highlight=config.EVENT_SERVER):
                    report += ["![slow server traces](queue_traces_slow_server.png)", ""]

        report += ["## Other metrics", "",
                   "![mean](mean_vs_staleness.png)", "", "![p95](p95_vs_staleness.png)", "",
                   "![max queue](max_queue_vs_staleness.png)", "", "![spread](cov_vs_staleness.png)", ""]
        report += [f"![{f}]({f})\n" for f in figs]
        report += decision_table(runs)
        report += sanity_section(runs)
    else:
        print(f"no testbed results at {runs_csv} (run: python -m lbstudy.sweep)")

    report += simulation_sections(sim_dir, out, agg)
    (out / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(f"wrote figures, tables and report.md to {out}")


if __name__ == "__main__":
    main()
