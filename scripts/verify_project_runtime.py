#!/usr/bin/env python3
"""Mandatory Linux container qualification using an explicit owner profile.

Preserves the original real-container acceptance assertions. Unlike the former
optional pytest entry, a missing daemon/profile is a failed qualification, never
a skip or a claim of success. CI runs this from the exact public release archive.
No implicit image pull, native fallback, permanent node config or user-project edits.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from agent.runtime_config import read_json
from scripts.check_release import check_archive, public_files
from shared.util import atomic_json

from shared.runtime_qualification import CHECKS


def project_request(root, tool, **args):
    from shared.contracts import TOOLS
    from shared.tool_protocol import wire_version
    values = {'project': 'fixture', 'idempotency_key': uuid.uuid4().hex, **args}
    return {'id': uuid.uuid4().hex, 'tool': tool, 'tool_contract_version': wire_version(tool),
        'project': {'root': str(root), 'alias': 'fixture', 'mode': 'write', 'allow_tasks': True},
        'args': TOOLS[tool].model.model_validate(values).model_dump()}


def shell_request(root, command='printf ok', **overrides):
    return project_request(root, 'exec', command=command, **overrides)


def canary_snapshot(path):
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode), 'Outside canary must remain a regular file'
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest())


def prepare_canaries(directory, root):
    """The trusted harness owns both sides; only root is granted to the Agent."""
    directory.chmod(0o755)
    targets, originals = [], {}
    for label in ('host-home', 'other-project'):
        outside = directory / label
        outside.mkdir()
        outside.chmod(0o755)
        canary = outside / 'canary.txt'
        canary.write_text('Harmless boundary fixture: ' + label, encoding='utf-8')
        # Do not let a mismatched workload UID turn a leaked mount into a false
        # pass. These synthetic files contain no credentials or user content.
        canary.chmod(0o666)
        originals[canary] = canary_snapshot(canary)
        targets.append(str(canary))
        for kind, destination in [('absolute', canary), ('relative', Path('..') / label / canary.name)]:
            link = root / (label + '-' + kind)
            link.symlink_to(destination)
            targets.append('/workspace/' + link.name)
    return targets, originals


def assert_canaries_unchanged(originals):
    for path, expected in originals.items():
        assert canary_snapshot(path) == expected, 'Outside canary changed during qualification'


def boundary_source(targets, endpoint_path):
    """Probe synthetic external targets, never real host credentials."""
    return ("import pathlib\n"
        + 'targets = ' + repr(targets) + '\n'
        + "for name in targets:\n"
        + "    path = pathlib.Path(name)\n"
        + "    for mode in ('rb', 'r+b'):\n"
        + "        try:\n"
        + "            with path.open(mode) as stream:\n"
        + "                stream.read(1) if mode == 'rb' else stream.write(b'forbidden')\n"
        + "        except (FileNotFoundError, PermissionError, NotADirectoryError):\n"
        + "            continue\n"
        + "        raise AssertionError('outside fixture became accessible')\n"
        + 'for name in ' + repr(['/var/run/docker.sock', endpoint_path]) + ':\n'
        + "    assert not pathlib.Path(name).exists(), 'management socket is exposed'\n"
        + "print('real-boundary-verified')\n")


def original_result(agent, request):
    status = agent.journal.status(request['id'])
    assert status['status'] == 'done' and status['result'] is not None, status
    result = status['result']
    assert result['ok'], result
    return result['data']


async def verify_container(profile, tmp_path, *, observations=None):
    import shlex
    from agent.config import validate_config
    from agent.runner import Agent
    from shared.crypto import token
    assert profile.get('mode') == 'container_v1', 'The real gate requires a container profile, never native fallback'
    root = (tmp_path / 'project').resolve()
    root.mkdir()
    targets, canaries = prepare_canaries(tmp_path, root)
    config_path = tmp_path / 'config.json'
    raw = {'hub_url': 'http://127.0.0.1:9', 'device_id': 'fixture', 'secret': token(),
           'state_dir': str((tmp_path / 'state').resolve()),
           'allowed_roots': [{'path': str(root), 'writable': True, 'allow_tasks': True}],
           'shell': {'enabled': True, 'projects': ['fixture'], 'command': ['/bin/sh', '-c']},
           'project_runtimes': {'fixture': profile}}
    atomic_json(config_path, validate_config(raw, config_path))
    agent = Agent(config_path)
    active = None
    active_request = None
    proofs, original_ids = [], []
    if observations is not None:
        observations.update(operations=proofs, original_operation_ids=original_ids)

    def remember_request(request):
        if request['id'] not in original_ids:
            original_ids.append(request['id'])

    async def submit(request):
        remember_request(request)
        await agent.handle(request)

    def workload_result(request, *, expected_exit=None):
        data = original_result(agent, request)
        assert data['execution_isolation']['state'] == 'verified_for_operation', data
        assert data['execution_isolation']['driver'] == 'docker_restricted_v1', data
        assert data['runtime']['mode'] == 'container_v1', data
        assert not data['runtime_cleanup']['pending'], data
        assert re.fullmatch(r'[a-f0-9]{64}', data['runtime']['revision']), data
        if expected_exit is not None:
            assert data['exit_code'] == expected_exit, data
            assert data['runtime_outcome']['state'] == 'confirmed', data
            assert data['runtime_outcome']['workload_exit_code'] == expected_exit, data
        proofs.append({'operation_id': request['id'], 'runtime': data['runtime'],
            'execution_isolation': data['execution_isolation'],
            'runtime_outcome': data['runtime_outcome'], 'runtime_cleanup': data['runtime_cleanup']})
        return data

    source = (
        "import os,pathlib,socket; assert os.geteuid()!=0; assert pathlib.Path.cwd()==pathlib.Path('/workspace'); "
        "assert os.environ['HOME']=='/home/project'; "
        "assert pathlib.Path('input.txt').read_text()=='before'; pathlib.Path('output.txt').write_text('after'); "
        "p=pathlib.Path('count'); p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
        "s=socket.socket(); s.settimeout(.2); assert s.connect_ex(('1.1.1.1',443))!=0; s.close(); "
        "print('real-container-verified')"
    )
    try:
        written = project_request(root, 'write', path='input.txt', content='before', expected_sha256='new')
        await submit(written)
        assert original_result(agent, written)['sha256'] == hashlib.sha256(b'before').hexdigest()
        request = shell_request(root, shlex.join(['python3', '-c', source]), timeout_seconds=60)
        await submit(request)
        data = workload_result(request, expected_exit=0)
        assert 'real-container-verified' in data['output'], data
        read = project_request(root, 'read', path='output.txt')
        await submit(read)
        output = original_result(agent, read)
        assert output['content'] == 'after' and output['sha256'] == hashlib.sha256(b'after').hexdigest()
        saved = agent.journal.status(request['id'])
        for _ in range(3):
            assert agent.journal.status(request['id']) == saved
        await submit(request)
        assert agent.journal.status(request['id']) == saved
        assert (root / 'count').read_text() == 'x'
        boundary = shell_request(root, shlex.join(['python3', '-c',
            boundary_source(targets, profile['container']['endpoint'][7:])]), timeout_seconds=60)
        await submit(boundary)
        assert 'real-boundary-verified' in workload_result(boundary, expected_exit=0)['output']
        assert_canaries_unchanged(canaries)
        failed = shell_request(root, 'exit 7', timeout_seconds=60)
        await submit(failed)
        workload_result(failed, expected_exit=7)
        timed = shell_request(root, 'sleep 30; touch must-not-exist', timeout_seconds=10)
        await submit(timed)
        assert workload_result(timed)['timed_out']
        assert not (root / 'must-not-exist').exists()
        cancelled = shell_request(root,
            'printf started > cancel-started; (sleep 30; touch cancel-child-escaped) & wait; touch cancel-finished',
            timeout_seconds=60)
        active_request = cancelled
        remember_request(cancelled)
        active = asyncio.create_task(agent.handle(cancelled))
        async with asyncio.timeout(60):
            while not (root / 'cancel-started').exists():
                assert not active.done(), 'Cancellation fixture never started'
                await asyncio.sleep(.05)
        agent.cancel_call(cancelled['id'])
        await asyncio.wait_for(asyncio.shield(active), 90)
        assert workload_result(cancelled)['cancelled']
        assert not (root / 'cancel-finished').exists()
        assert not (root / 'cancel-child-escaped').exists()
        assert_canaries_unchanged(canaries)
        assert len({item['runtime']['revision'] for item in proofs}) == 1
        assert len({item['runtime']['runtime_ref'] for item in proofs}) == 1
        if observations is not None:
            observations.update(operations=proofs, image=profile['container']['image'])
        return {'non_root': True, 'workspace_mapping': True, 'shared_read_write': True,
                'network_denied': True, 'immutable_image': True, 'inspection_before_start': True,
                'original_operation_replay_safe': True, 'workload_exit_code': True,
                'timeout_enforced': True, 'cleanup_confirmed': True,
                'host_absolute_path_denied': True, 'sibling_project_denied': True,
                'cross_boundary_link_denied': True, 'management_socket_denied': True,
                'cancellation_enforced': True, 'original_operation_query_safe': True}
    finally:
        if active is not None and not active.done():
            agent.cancel_call(active_request['id'])
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        if agent.kill_tasks:
            await asyncio.gather(*list(agent.kill_tasks), return_exceptions=True)
        if observations is not None:
            observations['operations'] = proofs
            observations['original_operation_ids'] = original_ids
        agent.journal.db.close()
        agent.instance_lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile-json', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--scratch-root', type=Path, help='Existing harness-owned temporary parent, outside protected Agent state')
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
    archive_hash = None
    scratch = None
    receipt = {'schema_version': 1, 'backend': 'docker_restricted_v1', 'verified': False,
        'commit': args.commit, 'archive_sha256': archive_hash,
        'source_inventory_sha256': hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
        'checks': {}, 'cleanup_pending': True, 'source_files_unchanged': False}
    try:
        # Reuse the release verifier: a caller-supplied ZIP hash/commit is not
        # evidence that the executing modules came from those archive bytes.
        archive_hash = check_archive(args.archive, before)['sha256']
        receipt['archive_sha256'] = archive_hash
        receipt['checks']['archive_matches_executed_source'] = True
        scratch = Path(tempfile.mkdtemp(prefix='codepier-runtime-qualification-', dir=args.scratch_root))
        observations = {}
        receipt['runtime_observations'] = observations
        receipt['checks'].update(asyncio.run(verify_container(profile, scratch, observations=observations)))
        after = public_files(ROOT)
        receipt['source_files_unchanged'] = before == after
        receipt['cleanup_pending'] = receipt['checks'].get('cleanup_confirmed') is not True
        receipt['verified'] = (receipt['source_files_unchanged'] and set(receipt['checks']) == CHECKS
            and all(value is True for value in receipt['checks'].values())
            and check_archive(args.archive, after)['sha256'] == archive_hash)
        if receipt['verified']:
            shutil.rmtree(scratch)
            scratch = None
    except BaseException as error:
        receipt['verified'] = False
        receipt['failure_type'] = type(error).__name__
        if scratch is not None:
            receipt['fixture_retained'] = True
            receipt.setdefault('runtime_observations', {})['fixture_directory'] = str(scratch)
            print('Original runtime qualification fixture retained: ' + str(scratch), file=sys.stderr)
        # Preserve the original journal/control receipts when creation, outcome
        # or cleanup is unknown. Never remove the fixture while claiming recovery.
        atomic_json(args.output, receipt)
        raise
    if scratch is not None:
        receipt['fixture_retained'] = True
        receipt.setdefault('runtime_observations', {})['fixture_directory'] = str(scratch)
        print('Original runtime qualification fixture retained: ' + str(scratch), file=sys.stderr)
    atomic_json(args.output, receipt)
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
