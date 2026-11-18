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


def desktop_environment():
    allowed = {
        "PATH",
        "HOME",
        "USER",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "XDG_RUNTIME_DIR",
        "XDG_SESSION_TYPE",
        "WAYLAND_DISPLAY",
        "DISPLAY",
        "XAUTHORITY",
        "SYSTEMROOT",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "TEMP",
        "TMP",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def provider_argv(command, platform=None):
    if (platform or os.name) == "nt":
        return [sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--windows", *command]
    return [sys.executable, str(Path(__file__).resolve()), "--", *command]


async def spawn_provider(*command, **kwargs):
    windows = os.name == "nt"
    process = await asyncio.create_subprocess_exec(
        *provider_argv(command), start_new_session=not windows, **kwargs
    )
    process.codepier_supervised = True
    if windows:
        from agent.native_windows import ProcessJob

        # The wrapper cannot launch a provider until the Job owns its process.
        # The only byte consumed here is the private gate, before protocol I/O.
        try:
            job = ProcessJob(process.pid)
            process.codepier_job_closed = asyncio.create_task(asyncio.to_thread(job.wait_and_close))
            process.stdin.write(b"G")
            await process.stdin.drain()
        except BaseException:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            if hasattr(process, "codepier_job_closed"):
                await asyncio.shield(process.codepier_job_closed)
            raise
    return process


def supervise(command):
    if __package__ in (None, ""):
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
            time.sleep(0.025)
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


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--windows" and os.name == "nt":
        if os.read(0, 1) != b"G":
            raise SystemExit(CLEANUP_FAILED)
        raise SystemExit(
            subprocess.call(sys.argv[2:], stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
        )
    if len(sys.argv) < 3 or sys.argv[1] != "--":
        raise SystemExit(127)
    raise SystemExit(supervise(sys.argv[2:]))
