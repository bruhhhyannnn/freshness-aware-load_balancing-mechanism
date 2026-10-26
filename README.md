# A Freshness-Aware Load Balancer (FALB) for Stale Server Load Information

## Start here (plain version)

**What this is.** Five small servers, one load balancer and a traffic
generator, all running on one laptop. The load balancer decides which server
gets each request, using one of five strategies: Round Robin, Random,
Least-Loaded, Power of Two Choices, or **FALB**, the mechanism proposed here.

**FALB in three sentences.** It checks how old each server's load report is.
Between reports, it adds the requests it has sent since and subtracts the
ones the server has probably finished, so a server that looked idle stops
looking idle. The older the information, the less it trusts it: it compares
all servers, then two random ones, then just picks at random.

**The only commands you need:**

```powershell
python -m lbstudy.sweep      # runs the whole experiment (about 6.5 h; rerun to resume)
python -m lbstudy.analyze    # makes the graphs + results/analysis/report.md
```

**Follow-up experiment** (the professor's feedback: fast / moderate / slow
tasks, CPU utilization, idle time and memory of every server, and 1 to 150
servers; all at 90% load):

```powershell
pip install -r requirements.txt     # adds psutil (memory and CPU of each server)
python -m lbstudy.tasks             # times the three tasks on this laptop (~30 s)
python -m lbstudy.sweep --plan nodes      # 185 runs, about 4.5 h; rerun to resume
python -m lbstudy.analyze_nodes     # graphs + results/analysis_nodes/report.md
```

Each task compresses a block of text (128 KB, 512 KB or 2 MB). The servers
replay the measured times, so each one behaves like its own machine instead
of competing with the others for this laptop's cores. Their CPU utilization
is the share of time they spend processing. On this laptop the testbed stayed
accurate up to 150 servers (one balancer handling about 8,300 requests/s); at
250 the load generator fell behind.

**The three graphs to show** (in `results/analysis/`):

| Figure | What it shows |
|---|---|
| `p99_vs_staleness.png` | As load information gets older, Least-Loaded and P2C get much slower; FALB stays low. |
| `histograms_scenarios.png` | When one server slows down, Round Robin and Random give it an equal share anyway, and a second hump of very slow requests appears. FALB has no second hump. |
| `scenario_summary.png` | Slowest-1% response time of each strategy in each test scenario. |

**The result in one paragraph** (full experiment, 315 runs). FALB has the
lowest p99 in 11 of the 18 staleness × load settings. Once information is
0.5 s old or older, its p99 is 3.8–38× lower than Least-Loaded's (H1); with
fresh information it beats P2C by 9–28% (H2); and when one server slows
down, its p99 stays at about 400 ms while Round Robin and Random reach about
7,000 ms (H3). It does not win everywhere: plain Round Robin is 27–34%
better when information is 5 s old, 5–9% better at 50% load once
information is 0.5 s old or older, and 22% better when four balancers share
one snapshot. The ablation (switching FALB's parts off one at a time) shows
that most of FALB's benefit comes from the "add the requests I sent"
correction.

Everything below is the technical detail behind this.

---

Implementation of the case study *"A Freshness-Aware Load Balancing Mechanism
for Stale Server Load Information"* (Advanced Operating Systems and
Networking). The project has the two parts the case study asks for:

1. **Mechanism design**: FALB, a load-balancing strategy that knows how old
   its load information is and adjusts each decision to match
   (`lbstudy/strategies.py`, class `FreshnessAware`).
2. **Testing with metrics**: a real, reproducible Python testbed (five server
   processes, a load balancer, a load generator) that compares FALB with Round
   Robin, Random, Least-Loaded and the Power of Two Choices. A discrete-event
   simulator validates the testbed and adds an ablation study.

```
                      one TCP connection per hop, requests multiplexed by id
 ┌───────────────┐        ┌──────────────────────────┐        ┌───────────┐
 │ load generator│──────▶ │      load balancer       │──────▶ │ server 1  │ FIFO queue,
 │ open-loop,    │        │ RR | Random | LL | P2C | │──────▶ │ server 2  │ 1 worker,
 │ Poisson,      │ ◀───── │ FALB                     │──────▶ │ server 3  │ exp(20 ms)
 │ records       │        │ status reports polled    │──────▶ │ server 4  │ job times
 │ latencies     │        │ every T s  ◀──── (queue, timestamp, completed, busy)
 └───────────────┘        └──────────────────────────┘        └───────────┘
   (runner process; also        (own OS process; 4 of them      (five OS processes)
    fires scenario events)       in the shared-snapshot scenario)
```

## Quick start

```powershell
pip install -r requirements.txt          # Python 3.11+; servers/LB use only asyncio
python -m pytest -q                      # unit tests + short end-to-end runs (~2 min)

# Compare all five strategies on the real testbed
python -m lbstudy.run --strategy all --staleness 1 --load 0.9
python -m lbstudy.run --strategy all --scenario slow_server

# Simulation: pilot/validation and the extension (ablation, larger pools, several balancers)
python -m lbstudy.simulate validate      # ~1 min
python -m lbstudy.simulate extend        # ~15-30 min on 16 cores

# The full testbed experiment: 315 runs, about 6.5 hours, resumable
python -m lbstudy.sweep --dry-run        # show the plan
python -m lbstudy.sweep                  # run it (Ctrl+C to stop, rerun to resume)

# Figures, tables and hypothesis verdicts -> results/analysis/report.md
python -m lbstudy.analyze
```

`python -m lbstudy.sweep --quick` runs everything once with short windows
(about 45 minutes) into `results/testbed_quick/`. Analyze that with
`python -m lbstudy.analyze --testbed results/testbed_quick --out results/analysis_quick`.

## Part 1: the mechanism (FALB)

Every status report carries four numbers: queue length, the time the report
was produced, requests completed so far, and seconds spent busy so far.

| Component | What FALB does | Code |
|---|---|---|
| **Age tracking** | age of each server's information = now − report time | `FreshnessAware.pick` |
| **Stale correction** | keeps its own estimate of every queue: it starts at the reported length, rises by 1 for every request FALB sends, and falls at the server's *measured* processing rate, never below zero at any moment. A server that reported "empty" stops looking empty as soon as requests go to it, which defeats herding. | `on_report`, `on_dispatch`, `estimate` |
| **Measured rate** | rate = Δcompleted / Δbusy between reports, averaged over roughly the last 100 requests, so a server that slows down is soon treated as slower | `on_report`, `rate` |
| **Adaptive choice with timeout** | every report younger than A1 → compare **all** servers; older → compare **two random** servers; a server whose report is older than the timeout A2 is not trusted and only receives its equal 1/n share at random (all reports that old → pure Random) | `pick` |
| **CPU variant** (optional) | uses CPU utilization u (busy fraction over the last second) instead of queue length, converted to an equivalent queue with the M/M/1 relation u/(1−u) | `signal="cpu"` → strategy `falb_cpu` |
| **Turn-taking fallback** (design alternative) | servers whose reports timed out take turns instead of being picked at random | `fallback="turns"` → strategy `falb_turns` |

Thresholds: A1 = 0.1 s, A2 = 2 s (`lbstudy/config.py`). Pilot simulations
over A1 ∈ {0, 0.1, 0.5} and A2 ∈ {2, 5, 10, ∞} kept these values. A1 = 0.1 s
was best. A larger A2 only helped at the most extreme staleness (T = 5 s), and
a shorter timeout reacts faster to a server that really stops responding.

**What the pilot simulations found** (`python -m lbstudy.simulate extend`; see the
ablation section of the report):
- The stale correction does most of the work. Without it, FALB is barely
  better than the Power of Two Choices.
- With one balancer, the adaptive choice adds little, because the correction
  already removes herding. With several balancers sharing one snapshot, each
  correction sees only part of the traffic. There the random two-choice step
  measurably lowers the p99.
- CPU utilization is a poor signal (2–10× worse). A one-second average is
  itself stale information, which agrees with Prequal's "load is not what
  you should balance" [9].
- With very old information (T = 5 s) on identical servers, Round Robin
  beats FALB by about 30%, because FALB's timeout falls back to Random. The
  turn-taking fallback narrows this to 2–6% and changes nothing elsewhere.
  It is offered as an option because the case study specifies a random
  fallback.

**Implementation note for the write-up.** "Reported queue + requests sent −
age × rate, never below zero" must be applied *continuously* (step by step,
at every request sent), not once as a single formula. Otherwise a server that
was idle for part of the interval "banks" completions it never performed,
and the estimate goes to zero too early.

## Part 2: testing

| Scenario | Setting | What it tests |
|---|---|---|
| **Staleness** | T ∈ {0, 0.1, 0.5, 1, 2, 5} s × load ∈ {50, 80, 90} % | how each strategy degrades as information ages (H1, H2) |
| **Missing report** | server 3 stops answering status polls for 10 s, 20 s into the window (load 80 %, T = 0.5 s) | the timeout (H3) |
| **Slow server** | server 3's processing time doubles for 20 s (load 80 %, T = 0.5 s) | unequal servers; equal-share strategies build queues (H3) |
| **Shared snapshot** *(added)* | 4 balancers split the traffic and all see the same poll at the same moment (load 90 %, T = 1 s) | the setting the problem statement names ("several load balancer instances"), and the one where FALB's random two-choice step matters |

Every scenario runs all five strategies, 60 s after a 10 s warm-up,
repeated 3 times. That is 270 + 45 = 315 runs, about 6.5 hours, unattended.

Metrics per run (`results/testbed/runs.csv`): mean, p50, p95, p99, p99.9 and
max response time; p99 *during* the event; response-time histograms (from
the per-request data); max queue per server; coefficient of variation of
requests per server; throughput and timeouts; **decision time** (µs spent in
the strategy code); and FALB's share of decisions in each mode.

## How the case study maps to the code

| Case study | Implementation |
|---|---|
| Five backend servers on different ports, one request at a time | `lbstudy/server.py`: one asyncio process per server, FIFO queue, single worker |
| Processing time exponential, mean 20 ms | `random.expovariate(1/0.020)`, seeded per server |
| Status endpoint: queue length, timestamp, (CPU utilization) | `STATUS` → (queue, time, completed, busy); utilization = Δbusy/Δtime |
| LB polls every T s; baselines use only the latest report | `lbstudy/balancer.py`: `_poll_forever()` hands reports to the strategy |
| T = 0: fresh information per request | the balancer probes the candidate servers before each decision |
| Five strategies | `lbstudy/strategies.py` |
| Open-loop Poisson load generator | `lbstudy/loadgen.py`; latency measured from the *scheduled* send time |
| Server stops reporting / slows down mid-run | `CONTROL` messages from the runner (`experiment.py`) to the server |
| Unattended experiment | `lbstudy/sweep.py` (resumable, shuffled order, keeps Windows awake) |
| Graphs, histograms, H1–H3 verdicts | `lbstudy/analyze.py` → `results/analysis/report.md` |

## Design decisions (and why)

**FALB is evaluated in the same harness as the baselines.** All five
strategies implement the same four calls (`on_report`, `candidates`, `pick`,
`on_dispatch`), and the same classes run in the testbed and the simulator.

**Every run uses fresh processes.** Each run starts the servers and
balancer(s) as separate OS processes on OS-assigned ports, so no state leaks
between runs and the servers don't share a GIL.

**A small binary protocol instead of HTTP.** Every hop uses one persistent TCP
connection carrying length-prefixed frames with a request id
(`lbstudy/protocol.py`), multiplexed like HTTP/2 or gRPC. With HTTP/1.1, a
herding burst of a thousand queued requests would need a thousand sockets,
and that cost would end up in the latencies. `TCP_NODELAY` is set.
*(The case study's tool list mentions aiohttp/HTTP; this is the reason for
the change.)*

**The baselines' snapshot really is stale.** Between polls, the baselines see
only the last reports, exactly as in Mitzenmacher's periodic update model
[7]. FALB's advantage comes only from what it adds, its own record of
requests sent, which is the point of the study.

**Ties are broken at random.** Otherwise Least-Loaded would send every tie to
server 1 and herding would be built into the code.

**Servers sleep until an absolute deadline.** A job's completion time is
`max(arrival, previous completion) + service time`. A late OS wake-up
delays one reply, but the server's true service rate stays exact.

**CPU utilization is measured as the worker's busy fraction.** Jobs are
simulated by sleeping, so `psutil` would only measure the Python
interpreter's overhead, not the simulated work. The busy fraction is exactly
what CPU utilization would be for a CPU-bound server.

**Open-loop traffic, latency from the scheduled time** (avoids
"coordinated omission"). **Common random numbers**: seeds depend only on the
load level and repetition, so every strategy faces identical arrivals and
job sizes.

**Windows timer precision (an OS lesson in itself).** With Python 3.12 on
Windows, `asyncio.sleep(0.005)` overshot by 11–15 ms on average. There were
two causes: asyncio's clock (`GetTickCount64`, 15.6 ms resolution before
Python 3.13), and the 15.6 ms OS timer tick, which Windows 11 does not
shorten for power-throttled processes. `lbstudy/platform_tuning.py` uses a
`QueryPerformanceCounter` event loop, opts out of power throttling and
requests a 1 ms tick. That brought the mean overshoot below 1 ms (p99
≈ 2.5 ms).

## Outputs

```
results/
  testbed/runs.csv              one row per run (the primary data)
  testbed/raw/*.npz             per-request latencies + 50 ms queue traces per server
  simulation/validate.csv       simulator on the testbed's grid + scenarios (20 reps)
  simulation/ablation.csv       FALB with each component switched off, CPU variant
  simulation/pools.csv          20 and 100 servers
  simulation/balancers.csv      1, 2, 4 balancers sharing one snapshot
  analysis/report.md            verdicts for H1–H3, win table, scenario tables, decision times
  analysis/p99_vs_staleness.png ← the main result
  analysis/histograms_*.png     response-time distributions ("bell shape")
  analysis/scenario_*.png       scenario summary and timelines
  analysis/queue_traces*.png    herding and the slow server, per-server queue over time
  analysis/sim_*.png            validation, ablation, pools, balancers
```

## Running the full sweep well

- Plug in the laptop, set the Windows power mode to *Best performance*, and
  close heavy apps. The sweep keeps the machine awake on its own.
- Interrupting is safe: rows are written after every run, and rerunning the
  same command skips finished runs. Failures go to
  `results/testbed/logs/failures.log` and are retried on the next run.
- A subset: `python -m lbstudy.sweep --scenarios slow_server missing_report`.

## Limitations to state in the report

- Everything runs on one machine over loopback, so a status poll is nearly
  free. Across a real network, polls cost more and FALB's own record (which
  needs no network) is worth relatively more.
- FALB's correction assumes it sees the requests it sends; with several
  balancers each sees only its share. The shared-snapshot scenario measures
  exactly this.
- Report ages use the wall clock of one machine. Across machines, clock skew
  would need NTP, or the age could be measured from the poll's send time.
- At 90 % load a single 60 s window is noisy (p99 varies by about ±20 %
  between windows). The report marks which comparisons are clear, and the
  simulator repeats the scenarios 20 times.

## References

See the case study. Key sources: Mitzenmacher [6], [7]; Dahlin, "Interpreting
stale load information" (2000) [8], the closest prior idea; Wydrowski et
al., Prequal (NSDI 2024) [9]; Dean & Barroso, "The tail at scale" [1].
