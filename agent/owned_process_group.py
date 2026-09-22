"""Stop an owned POSIX process group before reaping its leader.

An unreaped direct child pins its PID/PGID: cleanup never signals a historical
or potentially recycled PID. Zombies cannot produce side effects and are left
to their actual parent to reap. Windows terminal workers use their Job Object.
"""
from __future__ import annotations
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def child_exited(pid: int) -> bool:
    """Observe exit without releasing the process identity needed for cleanup."""
    return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def live_group_members(pgid: int) -> list[int]:
    if sys.platform.startswith('linux'):
        members = []
        for entry in Path('/proc').iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                raw = (entry / 'stat').read_text(encoding='utf-8')
            except (FileNotFoundError, ProcessLookupError):
                continue
            fields = raw.rsplit(')', 1)[1].split()
            if int(fields[2]) == pgid and fields[0] not in {'Z', 'X'}:
                members.append(int(entry.name))
        return members
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,pgid=,stat='],
                            capture_output=True, text=True, timeout=1, check=True)
    return [int(fields[0]) for line in result.stdout.splitlines()
            if len(fields := line.split()) >= 3 and int(fields[1]) == pgid
            and not fields[2].startswith(('Z', 'X'))]


def stop_owned_group(pid: int, grace: float = 2, kill_timeout: float = 3, *, grouped: bool | None = None) -> tuple[bool, int | None]:
    """Return success only after the owned group has no executing members."""
    code = None
    try:
        # Also proves this is still our unreaped direct child, before any signal.
        child_exited(pid)
        # Darwin hides getpgid() after exit even while our child is unreaped.
        # pty.fork/start_new_session workers pass their already-proven group.
        if grouped is None:
            grouped = os.getpgid(pid) == pid
        def send(number):
            nonlocal grouped
            grouped = grouped or os.getpgid(pid) == pid
            try:
                if grouped:
                    os.killpg(pid, number)
                else:
                    os.kill(pid, number)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Darwin returns EPERM for a group containing only an unreaped
                # zombie. Verify emptiness; a live inaccessible member still fails.
                if not child_exited(pid) or live_group_members(pid):
                    raise
        send(signal.SIGTERM)
        deadline = time.monotonic() + grace
        while not child_exited(pid) and time.monotonic() < deadline:
            time.sleep(.02)
        # The leader exiting must NOT bypass escalation for its descendants.
        send(signal.SIGKILL)
        deadline = time.monotonic() + kill_timeout
        while True:
            exited = child_exited(pid)
            members = live_group_members(pid) if grouped else ([] if exited else [pid])
            if exited and not members:
                _, status = os.waitpid(pid, 0)
                return True, os.waitstatus_to_exitcode(status)
            if time.monotonic() >= deadline:
                break
            time.sleep(.02)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        # Unknown cleanup is never reported as a successful stop.
        pass
    try:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            code = os.waitstatus_to_exitcode(status)
    except ChildProcessError:
        pass
    return False, code
