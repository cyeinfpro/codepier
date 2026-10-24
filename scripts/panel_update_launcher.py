#!/usr/bin/env python3
"""Stable host launcher; only administrator installation changes this program."""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import subprocess
import sys

try:
    from .panel_update_engine import EngineError, RESTART_EXIT, pending_jobs, secure_path, select_engine, verify_engine
except ImportError:
    from panel_update_engine import EngineError, RESTART_EXIT, pending_jobs, secure_path, select_engine, verify_engine


def child_command(root, home, identity, descriptor, *, expected_uid=0):
    directory = verify_engine(home, identity, expected_uid=expected_uid)
    interpreter = secure_path(Path(sys.executable).resolve(), expected_uid=expected_uid)
    return [str(interpreter), '-I', '-B', str(directory / 'panel_updater.py'), 'serve',
            '--root', str(root), '--supervised-lock-fd', str(descriptor), '--engine-id', identity['id']]


def supervise(root):
    if os.geteuid() != 0:
        raise EngineError('HOST_SETUP_REQUIRED', 'Host launcher requires administrator installation')
    root = secure_path(Path(root).resolve(), directory=True)
    home = secure_path(root / '.codepier-updater', directory=True)
    descriptor = os.open(home / 'service.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            identity = select_engine(home)
            command = child_command(root, home, identity, descriptor)
            result = subprocess.run(command, cwd=home / 'service', pass_fds=(descriptor,),
                                    env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8', 'HOME': '/root'})
            if result.returncode != RESTART_EXIT:
                return result.returncode
            if pending_jobs(home):
                raise EngineError('ENGINE_RECOVERY_REQUIRED', 'Engine requested restart with unfinished operations')
            selected = select_engine(home)
            if selected['id'] == identity['id']:
                raise EngineError('ENGINE_RESTART', 'Engine restart requires a verified new selector')
            # No fallback after the next engine runs: it may have accepted work.
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    try:
        return supervise(args.root)
    except (EngineError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
