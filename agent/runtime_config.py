"""Owner CLI for one explicitly selected project runtime; no grant/opt-in changes.

python -m agent.runtime_config doctor --config CONFIG --project NAME --root ROOT
python -m agent.runtime_config configure ... --profile-json PROFILE [--apply --expected-sha256 SHA]

Doctor and default configure are read-only. Apply requires a current full-file
SHA, an exclusive cooperating-writer lock, an owned regular config file and a
retained private backup. This is not a replacement for OS file permissions.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid

from agent.config import validate_config
from agent.project_runtime import validate_runtimes, runtime_readiness
from shared.util import DevError

MAX_BYTES = 1024 * 1024


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate configuration keys are not supported')
        value[key] = item
    return value


def _file_version(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def read_json(path):
    path = Path(path).absolute()
    if path.is_symlink():
        raise ValueError('Configuration inputs must not be symbolic links')
    # A FIFO substituted for the config must fail, not block a control worker.
    flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
             | getattr(os, 'O_BINARY', 0))
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError('Configuration input must be a bounded regular file')
        with os.fdopen(os.dup(descriptor), 'rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Configuration input exceeds its size limit')
        latest = os.fstat(descriptor)
        named = path.lstat()
        # Compare timestamps through the same descriptor API. Path-stat and
        # fd-stat timestamp representations can differ on Windows. Identity
        # still binds the current name to the originally opened object.
        if (_file_version(latest) != _file_version(info)
                or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino)
                or not stat.S_ISREG(named.st_mode) or named.st_size != info.st_size):
            raise DevError('RUNTIME_CONFIG_CHANGED', 'Configuration changed during the read; no write was performed', 409)
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise ValueError('Configuration input must be a JSON object')
        return value, raw, info
    finally:
        os.close(descriptor)


def _sync_directory(path):
    # POSIX directory fsync confirms replacement metadata; Windows relies on its
    # atomic replace + flushed files and does not support this directory call.
    if os.name == 'posix':
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _replace_config(path, original, original_info, replacement, expected):
    from agent.runtime_config_lock import config_lock
    if len(replacement) > MAX_BYTES:
        raise ValueError('Replacement configuration exceeds its readable size limit')
    if hashlib.sha256(original).hexdigest() != expected:
        raise DevError('SHA_CONFLICT', 'Configuration changed since review; no settings were changed', 409)
    if hasattr(os, 'getuid') and original_info.st_uid != os.getuid():
        raise ValueError('Only the configuration file owner may apply runtime settings')
    path = Path(path).absolute()
    temporary = None
    replaced = False
    with config_lock(path):
        try:
            _, latest, info = read_json(path)
            if ((info.st_dev, info.st_ino) != (original_info.st_dev, original_info.st_ino)
                    or hashlib.sha256(latest).hexdigest() != expected):
                raise DevError('SHA_CONFLICT', 'Configuration changed before apply; no settings were changed', 409)
            backup = path.with_name(path.name + '.runtime-backup-' + uuid.uuid4().hex)
            with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
            _sync_directory(path.parent)
            fd, name = tempfile.mkstemp(prefix=path.name + '.runtime-', dir=path.parent)
            temporary = Path(name)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(replacement)
                stream.flush()
                os.fsync(stream.fileno())
            _, latest, info = read_json(path)
            if ((info.st_dev, info.st_ino) != (original_info.st_dev, original_info.st_ino)
                    or hashlib.sha256(latest).hexdigest() != expected):
                raise DevError('SHA_CONFLICT', 'Configuration changed during preparation; original settings remain', 409)
            os.replace(temporary, path)
            replaced = True
            temporary = None
            _sync_directory(path.parent)
            return backup.name
        except OSError as exc:
            if replaced:
                raise DevError('DURABILITY_UNCONFIRMED',
                    '配置已替换，但持久落盘尚未确认；请核对原保存和当前配置，不要重复提交', 409) from exc
            raise
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass  # Retain owned scratch rather than mask the original outcome.


def inspect_or_configure(config_path, project_name, root, *, profile_path=None, apply=False, expected_sha256=''):
    path = Path(config_path).absolute()
    raw_value, raw, info = read_json(path)
    config = validate_config(raw_value, path)
    root = Path(root).resolve(strict=True)
    if not root.is_dir() or not any(root.is_relative_to(Path(row['path']).resolve()) for row in config['allowed_roots']):
        raise ValueError('Selected workspace must belong to an existing authorized Agent root')
    project = {'id': project_name, 'alias': project_name, 'root': str(root)}
    candidate = deepcopy(raw_value)
    if profile_path is not None:
        profile, _, _ = read_json(profile_path)
        value = validate_runtimes({project_name: profile})[project_name]
        candidate.setdefault('project_runtimes', {})[project_name] = value
        config = validate_config(candidate, path)
    elif apply:
        raise ValueError('Apply requires an explicitly reviewed profile JSON')
    result = {'schema_version': 1, 'project': project_name, 'applied': False,
        'config_sha256': hashlib.sha256(raw).hexdigest(), 'runtime': runtime_readiness(config, project, root),
        'grants_changed': False, 'shell_opt_in_changed': False, 'authorization': 'not_evaluated'}
    if apply:
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            raise ValueError('Apply requires --expected-sha256 from the reviewed configuration')
        replacement = (json.dumps(candidate, ensure_ascii=False, indent=2) + '\n').encode()
        backup = _replace_config(path, raw, info, replacement, expected_sha256)
        result.update(applied=True, backup_ref=backup, config_sha256=hashlib.sha256(replacement).hexdigest(),
                      activation='existing_agent_config_reload; recheck_authenticated_runtime_revision')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('doctor', 'configure'))
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--project', required=True)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--profile-json', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-sha256', default='')
    args = parser.parse_args(argv)
    if args.operation == 'doctor' and (args.profile_json or args.apply or args.expected_sha256):
        parser.error('doctor does not accept configuration mutations')
    if args.operation == 'configure' and args.profile_json is None:
        parser.error('configure requires --profile-json; without --apply it only previews')
    try:
        result = inspect_or_configure(args.config, args.project, args.root,
            profile_path=args.profile_json, apply=args.apply, expected_sha256=args.expected_sha256)
    except (OSError, ValueError, DevError) as exc:
        # Do not print config bodies, secret values, personal paths or environment.
        uncertain = getattr(exc, 'code', '') == 'DURABILITY_UNCONFIRMED'
        print(json.dumps({'state': 'needs_review' if uncertain else 'blocked',
                          'code': getattr(exc, 'code', 'RUNTIME_CONFIG_INVALID'),
                          'applied': None if uncertain else False, 'outcome_confirmed': not uncertain,
                          'message': 'Inspect the original save and current configuration; do not replay.' if uncertain else
                                     'Runtime configuration was not applied; inspect the selected inputs and current SHA.'}))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
