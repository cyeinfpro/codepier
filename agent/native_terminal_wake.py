"""An auto-reset command/output wakeup for a Windows terminal worker."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
import threading


class WindowsWake:
    def __init__(self, directory, session):
        import ctypes as c
        from ctypes import wintypes as w

        path = os.path.normcase(str(Path(directory).resolve()))
        key = sha256((path + "\0" + session).encode("utf-8")).hexdigest()
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateEventW": ([c.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR], w.HANDLE),
            "SetEvent": ([w.HANDLE], w.BOOL),
            "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
            "CloseHandle": ([w.HANDLE], w.BOOL),
        }
        for name, (argtypes, restype) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = argtypes, restype
        self.handle = self.kernel.CreateEventW(None, False, False, "Local\\CodePierNative-" + key)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        self.lock = threading.Lock()

    def set(self):
        with self.lock:
            if self.handle:
                self.kernel.SetEvent(self.handle)

    def wait(self, seconds):
        return self.kernel.WaitForSingleObject(self.handle, round(seconds * 1000)) == 0

    def close(self):
        with self.lock:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class LocalWake:
    # Use a one-bit event for controller tests on systems without Win32 events.
    def __init__(self):
        self._event = threading.Event()

    def set(self):
        self._event.set()

    def wait(self, seconds):
        self._event.wait(seconds)
        self._event.clear()


@contextmanager
def terminal_wake(directory, session):
    event = None
    if os.name == "nt":
        try:
            event = WindowsWake(directory, session)
        except OSError:
            # The durable queue still works with bounded polling if the event
            # is unavailable because of terminal-session or OS policy.
            pass
    try:
        yield event if event is not None else LocalWake()
    finally:
        if event is not None:
            event.close()


def signal_command(directory, session):
    if os.name == "nt":
        with terminal_wake(directory, session) as event:
            event.set()


def signal_backlog(wake, incoming):
    if not incoming.empty():
        wake.set()
