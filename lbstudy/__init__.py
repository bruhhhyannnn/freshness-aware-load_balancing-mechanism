"""Load balancing with stale information: a testbed for the Power of Two Choices.

Modules
-------
config            experiment parameters from the case study
strategies        Round Robin, Random, Least-Loaded, Power of d Choices
protocol          binary framing used on every TCP connection
server            backend server (single-worker FIFO queue, exponential service)
balancer          load balancer with periodically refreshed (stale) load snapshot
loadgen           open-loop Poisson load generator
experiment        runs one experiment end to end (spawns the processes)
sweep             runs the full factorial experiment unattended (resumable)
simulate          discrete-event simulator (validation + larger pools, d = 3)
analyze           tables, figures and hypothesis checks
"""

__version__ = "1.0.0"
