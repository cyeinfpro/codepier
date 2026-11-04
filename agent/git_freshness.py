"""Bounded local Git metadata only: no fetch, hooks, project code or writes."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time


def _read(root, args):
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            ['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=' + os.devnull,
             '-c', 'core.untrackedCache=false', '--no-optional-locks', *args],
            cwd=root, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
            timeout=3, check=False, env={**{key: value for key, value in os.environ.items()
                                          if not key.upper().startswith('GIT_')},
                                       'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0'})
        if result.returncode:
            raise ValueError('git metadata unavailable')
        output.seek(0)
        data = output.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError('git metadata exceeds budget')
        return data.decode('utf-8', 'replace').strip()


def freshness(root):
    root = Path(root)
    if not (root / '.git').exists() or (root / '.git').is_symlink():
        return {'available': False, 'reason': 'not_a_git_root', 'network_checked': False}
    try:
        status = _read(root, ['status', '--porcelain=v2', '--branch', '--untracked-files=normal'])
        result = {'available': True, 'branch': None, 'upstream': None, 'head': None,
                  'ahead': None, 'behind': None, 'dirty': 0, 'network_checked': False,
                  'last_fetch_age_seconds': None}
        for line in status.splitlines():
            if line.startswith('# branch.head '):
                result['branch'] = line[len('# branch.head '):]
            elif line.startswith('# branch.oid '):
                result['head'] = line[len('# branch.oid '):]
            elif line.startswith('# branch.upstream '):
                result['upstream'] = line[len('# branch.upstream '):]
            elif line.startswith('# branch.ab '):
                ahead, behind = line.split()[2:4]
                result['ahead'], result['behind'] = int(ahead), -int(behind)
            elif line and not line.startswith('#'):
                result['dirty'] += 1
        fetch_path = Path(_read(root, ['rev-parse', '--git-path', 'FETCH_HEAD']))
        if not fetch_path.is_absolute():
            fetch_path = root / fetch_path
        if fetch_path.is_file():
            result['last_fetch_age_seconds'] = max(0, int(time.time() - fetch_path.stat().st_mtime))
        if result['behind']:
            result['warning'] = 'Local checkout is behind its cached upstream; prefer a fresh automatic-base worktree. Dirty files were not changed.'
        return result
    except (OSError, subprocess.SubprocessError, ValueError):
        return {'available': False, 'reason': 'git_metadata_unavailable', 'network_checked': False}
