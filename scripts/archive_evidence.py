#!/usr/bin/env python3
"""List old test evidence by default; explicitly archive copies, never delete it.

Archives remain private and may contain sensitive test output. This is not a
production database, credentials, or native-session backup utility.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024


def inventory(directory, older_than):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Evidence root must be an existing non-symlink directory')
    selected = []
    for candidate in sorted(directory.iterdir()):
        if candidate.is_symlink():
            raise ValueError('Evidence contains a symlink')
        paths = [candidate]
        if candidate.is_dir():
            for current, directories, names in os.walk(candidate, followlinks=False):
                for name in [*directories, *names]:
                    item = Path(current) / name
                    if item.is_symlink():
                        raise ValueError('Evidence contains a symlink')
                    paths.append(item)
        if max(path.stat().st_mtime for path in paths) >= older_than:
            continue
        for path in paths:
            if path.name == 'EVIDENCE-MANIFEST.json':
                raise ValueError('An archive manifest cannot be used as evidence input')
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
                raise ValueError('Evidence must contain bounded regular files')
            # Do not turn this retention helper into a credentials/store backup.
            if path.name in {'master.key', 'master.keys.json', 'config.json', 'pairing.json', 'auth.json', '.env'} or path.suffix in {'.db', '.sqlite', '.sqlite3', '.key', '.pem'}:
                raise ValueError('Runtime credentials or databases require a dedicated backup process')
            selected.append({'path': path.relative_to(directory).as_posix(), 'bytes': info.st_size,
                             'identity': [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]})
    if sum(item['bytes'] for item in selected) > MAX_TOTAL:
        raise ValueError('Selected evidence exceeds the archive budget; select a smaller evidence root')
    return selected


def archive(directory, destination, selected):
    directory, destination = Path(directory), Path(destination)
    if destination.suffix != '.zip' or destination.exists() or destination.is_symlink():
        raise ValueError('Use a new ZIP destination; existing evidence is never overwritten')
    if destination.resolve().is_relative_to(directory.resolve()):
        raise ValueError('Archive must be outside the source evidence directory')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.evidence-', suffix='.zip', dir=destination.parent)
    os.close(descriptor)
    manifest = {}
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as output:
            for item in selected:
                relative = Path(item['path'])
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('Invalid evidence path')
                path = directory / relative
                if any(directory.joinpath(*relative.parts[:index]).is_symlink() for index in range(1, len(relative.parts) + 1)):
                    raise ValueError('Evidence source changed to a symlink')
                flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
                with os.fdopen(os.open(path, flags), 'rb') as stream:
                    before = os.fstat(stream.fileno())
                    identity = lambda value: [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]
                    if not stat.S_ISREG(before.st_mode) or identity(before) != item['identity']:
                        raise ValueError('Evidence changed after selection')
                    raw = stream.read(MAX_FILE + 1)
                    if len(raw) != item['bytes'] or identity(os.fstat(stream.fileno())) != item['identity'] or identity(path.lstat()) != item['identity']:
                        raise ValueError('Evidence changed during archive creation')
                manifest[item['path']] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                output.writestr(item['path'], raw)
            output.writestr('EVIDENCE-MANIFEST.json', json.dumps(manifest, sort_keys=True, indent=2) + '\n')
        with open(temporary, 'rb') as stream:
            os.fsync(stream.fileno())
        with zipfile.ZipFile(temporary) as check:
            for name, item in manifest.items():
                if hashlib.sha256(check.read(name)).hexdigest() != item['sha256']:
                    raise ValueError('Archived evidence failed verification')
        # Exclusive publication prevents overwriting a destination created while
        # the archive was being built; the same-directory temporary is owned.
        os.link(temporary, destination)
        return {'archive': str(destination), 'files': len(manifest), 'source_deleted': False,
                'sha256': hashlib.sha256(destination.read_bytes()).hexdigest()}
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / 'docs/evidence')
    parser.add_argument('--days', type=int, default=90)
    parser.add_argument('--archive', type=Path, help='Explicit new private ZIP path; omission only lists candidates')
    args = parser.parse_args()
    if not 1 <= args.days <= 36500:
        parser.error('--days must be between 1 and 36500')
    selected = inventory(args.directory, time.time() - args.days * 86400)
    result = {'dry_run': not args.archive, 'source_deleted': False, 'candidates': [{'path': item['path'], 'bytes': item['bytes']} for item in selected]}
    if args.archive:
        result.update(archive(args.directory, args.archive, selected))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
