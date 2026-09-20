"""Distinguish the loaded runtime from source files currently on disk."""
from __future__ import annotations
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from shared.util import VERSION

BASE = Path(__file__).resolve().parents[1]

IGNORED_DIRECTORIES = {'node_modules', '__pycache__', '.git', '.pytest_cache',
                       '.venv', '.venv-compat', 'data', 'private', '.work', 'dist'}
RUNTIME_SUFFIXES = {'.py', '.js', '.mjs', '.html', '.css', '.json', '.svg', '.zip', '.md'}


def source_identity(component: str, base: Path = BASE) -> dict:
    """Fingerprint shipped nested assets, not installed dependencies or state.

    This is an observation, not an OS-atomic snapshot. Any unreadable, missing,
    replaced or over-budget input makes completeness explicitly unverified.
    """
    import stat
    digest = hashlib.sha256()
    errors = []
    count = total = 0
    folders = ('shared', component) + (('web',) if component == 'hub' else ())

    def record(path):
        nonlocal count, total
        relative = path.relative_to(base).as_posix()
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_size > 8 * 1024 * 1024:
                raise OSError('Not a bounded regular runtime file')
            with path.open('rb') as stream:
                pinned = os.fstat(stream.fileno())
                if (before.st_dev, before.st_ino) != (pinned.st_dev, pinned.st_ino):
                    raise OSError('Runtime file was replaced')
                raw = stream.read(8 * 1024 * 1024 + 1)
                after = os.fstat(stream.fileno())
            if (before.st_mtime_ns, before.st_size, before.st_mode) != (after.st_mtime_ns, after.st_size, after.st_mode):
                raise OSError('Runtime file changed during observation')
            total += len(raw)
            count += 1
            if total > 64 * 1024 * 1024 or count > 10000:
                raise OSError('Runtime fingerprint budget exceeded')
            digest.update(relative.encode('utf-8') + b'\0')
            digest.update(str(stat.S_IMODE(before.st_mode)).encode() + b'\0')
            digest.update(hashlib.sha256(raw).digest())
        except (OSError, UnicodeError):
            errors.append(relative)

    for folder in folders:
        directory = base / folder
        if directory.is_symlink() or not directory.is_dir():
            errors.append(folder)
            continue
        def scan_error(exc):
            errors.append(folder)
        for current, directories, names in os.walk(directory, followlinks=False, onerror=scan_error):
            parent = Path(current)
            kept = []
            for name in sorted(directories):
                if name in IGNORED_DIRECTORIES or name.startswith('.venv'):
                    continue
                child = parent / name
                if child.is_symlink():
                    errors.append(child.relative_to(base).as_posix())
                else:
                    kept.append(name)
            directories[:] = kept
            for name in sorted(names):
                path = parent / name
                if path.suffix in RUNTIME_SUFFIXES and name not in {'config.json', 'auth.json', 'credentials.json', 'pairing.json'}:
                    record(path)
            if count > 10000 or total > 64 * 1024 * 1024:
                break
    # Requirements also affect the next runtime; fixtures may omit these files.
    for name in ('requirements.txt', 'requirements-agent.txt', 'requirements-bridge.txt'):
        path = base / name
        if path.exists() or path.is_symlink():
            record(path)
    try:
        text = (base / 'shared/util.py').read_text(encoding='utf-8')
        match = re.search(r'^VERSION\s*=\s*["\x27]([^"\x27]+)', text, re.M)
        version = match[1] if match else None
        if version is None:
            errors.append('shared/util.py')
    except (OSError, UnicodeError):
        version = None
        errors.append('shared/util.py')
    return {'version': version, 'source_sha256': digest.hexdigest(), 'errors': sorted(set(errors))}

class BuildIdentity:
    def __init__(self, component: str):
        from shared.contracts import tool_definitions
        self.component = component
        self.loaded = {**source_identity(component), 'version': VERSION, 'started_at': time.time(),
                       'instance_id': uuid.uuid4().hex, 'pid': os.getpid()}
        self.catalog_sha256 = hashlib.sha256(json.dumps(tool_definitions(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def describe(self) -> dict:
        disk = source_identity(self.component)
        return {'component': self.component, 'runtime': dict(self.loaded), 'disk': disk,
                'restart_required': bool(disk['errors'] or self.loaded['errors'] or disk['source_sha256'] != self.loaded['source_sha256'] or disk['version'] != self.loaded['version']),
                'source_verified': not bool(disk['errors'] or self.loaded['errors']),
                'catalog_sha256': self.catalog_sha256,
                'note': 'Runtime identity is captured at process startup; source files on disk are checked separately. This does not attest a remote Git revision.'}
