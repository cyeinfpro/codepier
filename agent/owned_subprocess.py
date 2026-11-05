"""Async stdout with an unreaped POSIX leader until owned group cleanup.

asyncio's normal child watcher reaps leaders as soon as they exit, releasing
PGIDs before descendants have been stopped. This adapter observes with WNOWAIT
and delegates the only reap to the existing owned-process-group cleanup.
"""
from __future__ import annotations

import asyncio
import os
import subprocess

from agent.owned_process_group import stop_owned_group


class OwnedProcess:
    def __init__(self, process):
        self._process = process
        self.pid = process.pid
        self._observed = None
        self._released = False
        self._stop_lock = asyncio.Lock()
        self.stdout = asyncio.StreamReader()
        self._transport = None

    @property
    def returncode(self):
        if self._observed is None and not self._released:
            try:
                status = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                self._released = True
            else:
                if status is not None:
                    self._observed = (status.si_status if status.si_code == os.CLD_EXITED else -status.si_status)
        return self._observed

    async def wait(self):
        while self.returncode is None and not self._released:
            await asyncio.sleep(.02)
        return self.returncode

    async def stop_owned(self):
        async with self._stop_lock:
            if self._released:
                return
            observed = self.returncode
            if self._released:
                return
            work = asyncio.create_task(asyncio.to_thread(stop_owned_group, self.pid, grouped=True))
            cancellation = None
            try:
                _, code = await asyncio.shield(work)
            except asyncio.CancelledError as exc:
                # The native cleanup still owns the child. Settle it before
                # releasing our lock or allowing a second cleanup/reap.
                cancellation = exc
                while not work.done():
                    try:
                        await asyncio.shield(work)
                    except asyncio.CancelledError:
                        continue
                _, code = work.result()
            if code is not None:
                self._observed = observed if observed is not None else code
                self._released = True
                # Prevent Popen's destructor from polling a released identity.
                self._process.returncode = self._observed
            if cancellation is not None:
                raise cancellation


async def create_owned_process(*command, **options):
    process = await asyncio.to_thread(subprocess.Popen, command, **options)
    owned = OwnedProcess(process)
    try:
        loop = asyncio.get_running_loop()
        owned._transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(owned.stdout), process.stdout)
    except BaseException:
        await owned.stop_owned()
        raise
    return owned
