"""Bounded POSIX container-control subprocesses, separate from project output.

Read both pipes incrementally with one combined byte budget. A daemon or CLI
cannot allocate unbounded memory before an after-the-fact output-length check.
On timeout/overflow only the process group created here is terminated.
"""
from __future__ import annotations
import os
import selectors
import signal
import subprocess
import time

from shared.util import DevError


def run_control(command, environment, *, timeout=20.0, max_bytes=1024*1024):
    if os.name != 'posix':
        raise DevError('RUNTIME_BACKEND_UNSUPPORTED', 'Container control requires a POSIX execution node', 409)
    if timeout <= 0:
        raise DevError('RUNTIME_CONTROL_UNCERTAIN', 'Original runtime preparation budget expired', 409)
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=environment, start_new_session=True)
    output = bytearray()
    errors = bytearray()
    total = 0
    completed = False
    try:
        with selectors.DefaultSelector() as selector:
            for stream, buffer in ((process.stdout, output), (process.stderr, errors)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, buffer)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DevError('RUNTIME_CONTROL_UNCERTAIN', 'Container control timed out; inspect the original operation before further action', 409)
                for key, _ in selector.select(min(remaining, .25)):
                    block = os.read(key.fd, min(65536, max_bytes - total + 1))
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(block)
                    if total > max_bytes:
                        raise DevError('RUNTIME_CONTROL_OVERSIZED', 'Container control exceeded its combined output bound', 409)
                    key.data.extend(block)
            remaining = deadline - time.monotonic()
            try:
                code = process.wait(timeout=max(.001, remaining))
            except subprocess.TimeoutExpired as error:
                raise DevError('RUNTIME_CONTROL_UNCERTAIN', 'Container control did not exit within its budget', 409) from error
            completed = True
            return subprocess.CompletedProcess(command, code, bytes(output), bytes(errors))
    finally:
        if not completed:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired as error:
                raise DevError('RUNTIME_CONTROL_UNCERTAIN', 'Container control process cleanup is unconfirmed', 409) from error
        for stream in (process.stdout, process.stderr):
            stream.close()
