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


def live_group_members(pgid: int, *, timeout: float = 1) -> list[int]:
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
    # Darwin supports selection by process group. Do not inspect every host
    # process just to verify one owned group: unrelated load must not consume
    # the short cleanup-observation budget. Linux uses /proc above because its
    # ps -g option has different semantics.
    command = (['/bin/ps', '-x', '-g', str(pgid), '-o', 'pid=,pgid=,stat=']
               if sys.platform == 'darwin' else ['/bin/ps', '-axo', 'pid=,pgid=,stat='])
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=True)
    members = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 3:
            raise ValueError('Incomplete process-group observation')
        identifier, group = int(fields[0]), int(fields[1])
        if group == pgid and not fields[2].startswith(('Z', 'X')):
            members.append(identifier)
    return members


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
            if not grouped:
                try:
                    grouped = os.getpgid(pid) == pid
                except ProcessLookupError:
                    if child_exited(pid):
                        return
                    raise
            try:
                if grouped:
                    os.killpg(pid, number)
                else:
                    os.kill(pid, number)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Darwin returns EPERM for an unreaped zombie-only group.
                # This is NOT cleanup proof: the bounded survey below must still
                # observe no live descendants before releasing the leader PID.
                if not grouped or not child_exited(pid):
                    raise
        if not child_exited(pid):
            send(signal.SIGTERM)
        deadline = time.monotonic() + grace
        while not child_exited(pid) and time.monotonic() < deadline:
            time.sleep(.02)
        # The leader exiting must NOT bypass escalation for its descendants.
        send(signal.SIGKILL)
        deadline = time.monotonic() + kill_timeout
        while True:
            exited = child_exited(pid)
            try:
                remaining = max(.001, min(1.0, deadline - time.monotonic()))
                members = live_group_members(pid, timeout=remaining) if grouped else ([] if exited else [pid])
            except (OSError, ValueError, IndexError, subprocess.SubprocessError):
                # A process-list race or transient survey timeout is unknown,
                # not an empty group. Keep the PID pinned and observe again
                # within the original deadline; never replay a stop command.
                members = None
            if exited and members == []:
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
