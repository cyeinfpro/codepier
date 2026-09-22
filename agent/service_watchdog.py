"""Detect a stalled service event loop independently of the Hub connection."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
import sys
import threading
import time


class Watchdog:
    def __init__(self, *, timeout=120.0, interval=5.0, clock=time.monotonic,
                 terminate=os._exit):
        self.timeout = timeout
        self.interval = interval
        self.clock = clock
        self.terminate = terminate
        self.last_progress = clock()
        self.last_check = self.last_progress
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.monitor, name="agent-watchdog", daemon=True)

    def beat(self):
        self.last_progress = self.clock()

    def stalled(self):
        now = self.clock()
        # Suspend/resume can advance the monotonic clock on Windows. Give the
        # event loop a full grace period after the whole machine was suspended.
        if now - self.last_check > max(self.interval * 4, 30):
            self.last_progress = now
        self.last_check = now
        return now - self.last_progress >= self.timeout

    def monitor(self):
        while not self.stopped.wait(self.interval):
            if self.stalled():
                # Exiting is deliberate: Task Scheduler owns restart/backoff.
                # Do not wait for the event loop or stdout locks on this path.
                try:
                    os.write(sys.stderr.fileno(), b"CodePier: event loop stalled; restarting service.\n")
                except (AttributeError, OSError, ValueError):
                    pass
                self.terminate(1)
                return

    def close(self):
        self.stopped.set()
        if self.thread.is_alive():
            self.thread.join(timeout=self.interval + 1)


@contextmanager
def watch_event_loop():
    loop = asyncio.get_running_loop()
    watchdog = Watchdog()
    timer = None

    def beat():
        nonlocal timer
        watchdog.beat()
        timer = loop.call_later(min(10, watchdog.timeout / 4), beat)

    beat()
    watchdog.thread.start()
    try:
        yield
    finally:
        if timer is not None:
            timer.cancel()
        watchdog.close()


def run_service(base):
    """Windowless Task Scheduler entry point; the scheduler owns this process."""
    from pathlib import Path
    base = Path(base)
    logs = base / "logs"
    logs.mkdir(exist_ok=True)
    sys.stdout = (logs / "stdout.log").open("a", buffering=1, encoding="utf-8")
    sys.stderr = (logs / "stderr.log").open("a", buffering=1, encoding="utf-8")
    os.chdir(base / "runtime")
    sys.argv = ["agent", "--config", str(base / "config.json"), "run", "--supervised"]
    from agent.__main__ import main
    main()
