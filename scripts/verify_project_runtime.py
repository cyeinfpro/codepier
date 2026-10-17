#!/usr/bin/env python3
"""Mandatory Linux container qualification using an explicit owner profile.

Preserves the original real-container acceptance assertions. Unlike the former
optional pytest entry, a missing daemon/profile is a failed qualification, never
a skip or a claim of success. CI runs this from the exact public release archive.
No implicit image pull, native fallback, permanent node config or project edits.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from agent.runtime_config import read_json
from scripts.check_release import public_files
from shared.util import atomic_json

from shared.runtime_qualification import CHECKS


def shell_request(root, command='printf ok', **overrides):
    return {'id': uuid.uuid4().hex, 'tool': 'shell_exec',
        'project': {'root': str(root), 'alias': 'fixture', 'mode': 'write', 'allow_tasks': True},
        'args': {'project': 'fixture', 'command': command, 'idempotency_key': uuid.uuid4().hex, **overrides}}


async def verify_container(profile, tmp_path):
    import shlex
    from agent.config import validate_config
    from agent.runner import Agent
    from shared.crypto import token
    from shared.util import atomic_json
    assert profile.get('mode') == 'container_v1', 'The real gate requires a container profile, never native fallback'
    root = (tmp_path / 'project').resolve()
    root.mkdir()
    (root / 'input.txt').write_text('before')
    config_path = tmp_path / 'config.json'
    raw = {'hub_url': 'http://127.0.0.1:9', 'device_id': 'fixture', 'secret': token(),
           'state_dir': str((tmp_path / 'state').resolve()),
           'allowed_roots': [{'path': str(root), 'writable': True, 'allow_tasks': True}],
           'shell': {'enabled': True, 'projects': ['fixture'], 'command': ['/bin/sh', '-c']},
           'project_runtimes': {'fixture': profile}}
    atomic_json(config_path, validate_config(raw, config_path))
    agent = Agent(config_path)
    source = (
        "import os,pathlib,socket; assert os.geteuid()!=0; assert pathlib.Path.cwd()==pathlib.Path('/workspace'); "
        "assert os.environ['HOME']=='/home/project'; assert not pathlib.Path('/var/run/docker.sock').exists(); "
        "assert pathlib.Path('input.txt').read_text()=='before'; pathlib.Path('output.txt').write_text('after'); "
        "p=pathlib.Path('count'); p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
        "s=socket.socket(); s.settimeout(.2); assert s.connect_ex(('1.1.1.1',443))!=0; s.close(); "
        "print('real-container-verified')"
    )
    try:
        request = shell_request(root, shlex.join(['python3', '-c', source]), timeout_seconds=60)
        await agent.handle(request)
        result = agent.journal.status(request['id'])['result']
        assert result['ok'], result
        data = result['data']
        assert data['exit_code'] == 0 and 'real-container-verified' in data['output'], data
        assert data['execution_isolation']['state'] == 'verified_for_operation'
        assert data['runtime_outcome']['state'] == 'confirmed'
        assert not data['runtime_cleanup']['pending']
        assert (root / 'output.txt').read_text() == 'after'
        await agent.handle(request)
        assert (root / 'count').read_text() == 'x'
        failed = shell_request(root, 'exit 7', timeout_seconds=60)
        await agent.handle(failed)
        actual = agent.journal.status(failed['id'])['result']['data']
        assert actual['exit_code'] == 7 and actual['runtime_outcome']['workload_exit_code'] == 7
        timed = shell_request(root, 'sleep 30; touch must-not-exist', timeout_seconds=10)
        await agent.handle(timed)
        timeout_result = agent.journal.status(timed['id'])['result']
        assert timeout_result['ok'], timeout_result
        assert timeout_result['data']['timed_out'] and not timeout_result['data']['runtime_cleanup']['pending']
        assert not (root / 'must-not-exist').exists()
        return {'non_root': True, 'workspace_mapping': True, 'shared_read_write': True,
                'network_denied': True, 'immutable_image': True, 'inspection_before_start': True,
                'original_operation_replay_safe': True, 'workload_exit_code': True,
                'timeout_enforced': True, 'cleanup_confirmed': True}
    finally:
        agent.journal.db.close()
        agent.instance_lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile-json', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--commit', required=True)
    args = parser.parse_args(argv)
    if not __debug__:
        parser.error('Qualification assertions require normal Python; optimized execution is forbidden')
    if not re.fullmatch(r'[a-f0-9]{40}', args.commit):
        parser.error('An exact source commit is required')
    if args.output.exists():
        parser.error('Use a new proof path; existing evidence is retained')
    profile, _, _ = read_json(args.profile_json)
    if profile.get('mode') != 'container_v1':
        parser.error('A real container_v1 profile is required')
    before = public_files(ROOT)
    archive_hash = hashlib.sha256(args.archive.read_bytes()).hexdigest()
    receipt = {'schema_version': 1, 'backend': 'docker_restricted_v1', 'verified': False,
        'commit': args.commit, 'archive_sha256': archive_hash,
        'source_inventory_sha256': hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
        'checks': {}, 'cleanup_pending': True, 'source_files_unchanged': False}
    try:
        with tempfile.TemporaryDirectory(prefix='codepier-runtime-qualification-') as scratch:
            receipt['checks'] = asyncio.run(verify_container(profile, Path(scratch)))
        after = public_files(ROOT)
        receipt['source_files_unchanged'] = before == after
        receipt['cleanup_pending'] = False
        receipt['verified'] = (receipt['source_files_unchanged'] and set(receipt['checks']) == CHECKS
            and all(value is True for value in receipt['checks'].values())
            and hashlib.sha256(args.archive.read_bytes()).hexdigest() == archive_hash)
    except Exception as error:
        receipt['failure_type'] = type(error).__name__
        # Retain the traceback in the scoped CI log; do not copy secrets/paths
        # into public proof. False remains false on any assertion or cleanup error.
        atomic_json(args.output, receipt)
        raise
    atomic_json(args.output, receipt)
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
