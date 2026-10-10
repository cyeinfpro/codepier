"""Own each POSIX desktop provider tree until its leader and descendants stop.

The asyncio parent owns this supervisor, which alone owns/signals/reaps the
provider group. Keeping that direct child unreaped prevents historical-PID
cleanup after asyncio has already released a provider's process identity.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

CLEANUP_FAILED = 125


def provider_argv(command, platform=None):
    if (platform or os.name) == 'nt':
        return list(command)
    return [sys.executable, str(Path(__file__).resolve()), '--', *command]


async def spawn_provider(*command, **kwargs):
    windows = os.name == 'nt'
    process = await asyncio.create_subprocess_exec(
        *provider_argv(command), start_new_session=not windows, **kwargs)
    process.codepier_supervised = not windows
    return process


def supervise(command):
    if __package__ in (None, ''):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent.owned_process_group import child_exited, stop_owned_group

    stopping = False

    def stop(*_args):
        nonlocal stopping
        stopping = True

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, stop)
    parent = os.getppid()
    child = None
    try:
        # Inherited protocol pipes preserve exact bytes without a relay/parser.
        child = subprocess.Popen(command, start_new_session=True)
        while not stopping and os.getppid() == parent and not child_exited(child.pid):
            time.sleep(.025)
    except OSError:
        return 127
    finally:
        if child is not None:
            cleaned, code = stop_owned_group(child.pid, grouped=True)
            # Never let Popen.__del__ reap again or signal a recycled identity.
            child.returncode = code if code is not None else CLEANUP_FAILED
    if not cleaned:
        return CLEANUP_FAILED
    return code if code is not None and code >= 0 else 128 - (code or 0)


if __name__ == '__main__':
    if len(sys.argv) < 3 or sys.argv[1] != '--':
        raise SystemExit(127)
    raise SystemExit(supervise(sys.argv[2:]))
