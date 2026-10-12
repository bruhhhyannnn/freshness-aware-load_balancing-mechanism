"""Give each testbed process an event loop whose timers are precise to ~1 ms.

The servers simulate 20 ms jobs by sleeping, so timer precision matters.
On Windows with Python 3.12 the stock setup is far too coarse, for two
separate reasons (measured on the development machine, ``asyncio.sleep``
overshot by 11-15 ms on average):

1. asyncio reads time from ``time.monotonic()``, which on Windows before
   Python 3.13 is ``GetTickCount64`` with a 15.6 ms resolution. The loop
   cannot schedule anything more finely than its own clock.
2. The OS wakes waiting threads on the system timer tick (15.6 ms by
   default). ``timeBeginPeriod(1)`` requests a 1 ms tick, but Windows 11
   ignores the request for processes it power-throttles, so the process
   also has to opt out of that throttling.

``run()`` fixes (1) with an event loop that reads ``time.perf_counter()``
(QueryPerformanceCounter, sub-microsecond), and ``tune_process()`` fixes
(2). Together they brought the mean overshoot to under 1 ms (p99 ~2.5 ms).
On Linux and macOS the stock event loop is already precise; both helpers
then change nothing.
"""

from __future__ import annotations

import asyncio
import sys
import time

_tuned = False


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _PowerThrottlingState(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG),
                    ("ControlMask", wintypes.ULONG),
                    ("StateMask", wintypes.ULONG)]

    _PROCESS_POWER_THROTTLING = 4            # PROCESS_INFORMATION_CLASS value
    _THROTTLE_EXECUTION_SPEED = 0x1
    _THROTTLE_IGNORE_TIMER_RESOLUTION = 0x4
    _ABOVE_NORMAL_PRIORITY_CLASS = 0x8000
    _ES_CONTINUOUS = 0x80000000
    _ES_SYSTEM_REQUIRED = 0x00000001

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.SetProcessInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
    _kernel32.SetThreadExecutionState.restype = wintypes.DWORD

    class PrecisionEventLoop(asyncio.ProactorEventLoop):
        """Proactor loop that schedules timers with QueryPerformanceCounter."""

        def __init__(self) -> None:
            super().__init__()
            self._clock_resolution = time.get_clock_info("perf_counter").resolution

        def time(self) -> float:
            return time.perf_counter()

    def _new_loop() -> asyncio.AbstractEventLoop:
        return PrecisionEventLoop()

else:
    _new_loop = asyncio.new_event_loop


def tune_process(raise_priority: bool = True) -> None:
    """Request 1 ms timer resolution (and slightly higher priority) on Windows."""
    global _tuned
    if _tuned or sys.platform != "win32":
        return
    _tuned = True
    me = _kernel32.GetCurrentProcess()
    # ControlMask selects the throttling features we set; StateMask 0 = off.
    state = _PowerThrottlingState(
        1, _THROTTLE_EXECUTION_SPEED | _THROTTLE_IGNORE_TIMER_RESOLUTION, 0)
    _kernel32.SetProcessInformation(me, _PROCESS_POWER_THROTTLING,
                                    ctypes.byref(state), ctypes.sizeof(state))
    ctypes.windll.winmm.timeBeginPeriod(1)
    if raise_priority:
        _kernel32.SetPriorityClass(me, _ABOVE_NORMAL_PRIORITY_CLASS)


def keep_awake(enabled: bool) -> None:
    """Stop Windows from sleeping during a long unattended sweep."""
    if sys.platform == "win32":
        flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if enabled else 0)
        _kernel32.SetThreadExecutionState(flags)


def run(coro):
    """``asyncio.run`` with the precise event loop and process tuning applied."""
    tune_process()
    with asyncio.Runner(loop_factory=_new_loop) as runner:
        return runner.run(coro)
