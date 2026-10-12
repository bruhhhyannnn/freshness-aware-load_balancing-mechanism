"""Experiment parameters, taken directly from the case study."""

N_SERVERS = 5
MEAN_SERVICE_S = 0.020                 # exponential processing time, mean 20 ms
SERVER_RATE = 1.0 / MEAN_SERVICE_S     # ~50 requests/s per server, ~250 for five

BASELINES = ("round_robin", "random", "least_loaded", "p2c")
STRATEGIES = BASELINES + ("falb",)

# FALB thresholds: compare all servers while every report is younger than A1,
# two random servers while older, and stop trusting a report older than A2.
FALB_A1 = 0.1
FALB_A2 = 2.0

# Scenario 1 (staleness): every strategy at every poll interval and load.
STALENESS_S = (0.0, 0.1, 0.5, 1.0, 2.0, 5.0)   # T = 0 means "read fresh per request"
LOADS = (0.5, 0.8, 0.9)                         # fraction of total capacity

# Scenarios 2-4, each at one load level and poll interval.
EVENT_SERVER = 2            # the server that misbehaves (0-based: the third)
EVENT_START_S = 20.0        # seconds into the measurement window
SCENARIOS = {
    # One server stops answering status polls for 10 s (it keeps processing).
    "missing_report": dict(load=0.8, staleness=0.5, event="mute", duration=10.0),
    # One server's processing time doubles for 20 s.
    "slow_server": dict(load=0.8, staleness=0.5, event="slow", duration=20.0, factor=2.0),
    # Four balancers share the traffic and see the same snapshot at the same
    # moments (as if fed by one central health checker); each one knows only
    # about the requests it sent itself.
    "shared_snapshot": dict(load=0.9, staleness=1.0, balancers=4),
}

WARMUP_S = 10.0
DURATION_S = 60.0
REPETITIONS = 3

# A request with no reply this long after it was scheduled counts as timed out.
TIMEOUT_S = 30.0

HOST = "127.0.0.1"
