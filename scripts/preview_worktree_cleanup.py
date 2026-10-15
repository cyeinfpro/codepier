#!/usr/bin/env python3
"""Read-only Git worktree inventory. A candidate is never deletion authority."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess


def git(root, *args):
    return subprocess.check_output(
        ['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
         '-c', 'core.pager=cat', '-C', str(root), *args],
        stderr=subprocess.DEVNULL, timeout=20)


def size(path, limit=100000):
    total = count = 0
    for root, directories, files in os.walk(path, followlinks=False):
        directories[:] = [d for d in directories if not (Path(root)/d).is_symlink()]
        for name in files:
            item = Path(root)/name
            count += 1
            if count > limit:
                return total, False
            st = item.lstat()
            if not item.is_symlink():
                total += st.st_size
    return total, True


def inventory(root, *, max_entries=100, file_limit=100000):
    root = Path(root).resolve(strict=True)
    entries = git(root, 'worktree', 'list', '--porcelain', '-z').split(b'\0\0')
    primary = Path(os.fsdecode(entries[0].split(b'\0')[0][9:]))
    result = []
    for block in entries[:max_entries]:
        fields = block.split(b'\0')
        values = dict(field.split(b' ', 1) for field in fields if b' ' in field)
        if b'worktree' not in values:
            continue
        path = Path(os.fsdecode(values[b'worktree']))
        row = {'path': str(path), 'head': values.get(b'HEAD', b'').decode(),
               'delete_authorized': False, 'reasons': ['live_use_and_task_ownership_not_verified']}
        if path == primary:
            row['reasons'].append('original_checkout')
        if path.is_symlink() or not path.is_dir() or path.resolve() != path:
            row['reasons'].append('missing_or_changed_path')
            result.append(row)
            continue
        st = path.stat()
        row['identity'] = [st.st_dev, st.st_ino]
        try:
            row['dirty'] = bool(git(path, 'status', '--porcelain=v1', '--untracked-files=all'))
            ignored = git(path, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z')
            row['ignored_count'] = len([item for item in ignored.split(b'\0') if item])
            refs = git(path, 'for-each-ref', '--format=%(refname)', '--contains=HEAD', 'refs/heads', 'refs/remotes').splitlines()
            row['head_retained_by_ref'] = bool(refs)
            row['bytes'], row['size_complete'] = size(path, file_limit)
            if row['dirty']:
                row['reasons'].append('dirty_or_untracked')
            if not refs:
                row['reasons'].append('head_not_retained_by_branch_or_remote_ref')
            if row['ignored_count']:
                row['reasons'].append('ignored_files_need_ownership_review')
            if not row['size_complete']:
                row['reasons'].append('size_scan_limit')
            row['candidate_for_review'] = not row['dirty'] and bool(refs) and path != primary
        except (OSError, subprocess.SubprocessError):
            row['reasons'].append('inspection_failed')
        result.append(row)
    return {'read_only': True, 'deletion_supported': False,
            'truncated': len(entries) > max_entries + 1, 'worktrees': result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--max-entries', type=int, default=100)
    parser.add_argument('--file-limit', type=int, default=100000)
    args = parser.parse_args()
    if not 1 <= args.max_entries <= 1000 or not 1 <= args.file_limit <= 1000000:
        parser.error('Limits must be positive and bounded')
    print(json.dumps(inventory(args.root, max_entries=args.max_entries,
                               file_limit=args.file_limit), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
