"""Synthetic daemon-contract checks; real Docker acceptance is explicitly separate."""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from agent.container_runtime import (ContainerRuntime, validate_container, create_arguments,
    verify_inspection, tmpfs_options, validate_mount_root, inspect_project_mount)
from agent.project_runtime import validate_runtimes, prepare
from shared.util import DevError

IMAGE = 'sha256:' + 'a' * 64
CID = 'b' * 64


@pytest.fixture
def container_plan(tmp_path):
    root = (tmp_path / 'project').resolve()
    root.mkdir()
    state = (tmp_path / 'state').resolve()
    state.mkdir()
    profile = {'mode': 'container_v1', 'shell': 'sh', 'container': {
        'engine': '/synthetic/docker', 'endpoint': 'unix:///synthetic/docker.sock', 'image': IMAGE}}
    config = {'state_dir': str(state), 'secret': 'synthetic-key',
              'project_runtimes': validate_runtimes({'fixture': profile})}
    project = {'alias': 'fixture', 'id': 'project', 'root': str(root)}
    plan = prepare(config, project, root, ['/bin/sh', '-c', 'exit 7'], root, create=False)
    return config, project, root, plan


def inspected(config, root, plan, name):
    c = config
    return {'Id': CID, 'Image': IMAGE, 'State': {'Running': False},
        'HostConfig': {'Privileged': False, 'ReadonlyRootfs': True, 'NetworkMode': 'none',
            'IpcMode': 'private', 'PidMode': '', 'UTSMode': '', 'CgroupnsMode': 'private',
            'PidsLimit': c['pids_limit'], 'Memory': c['memory_mib'] * 1024 * 1024,
            'MemorySwap': c['memory_mib'] * 1024 * 1024, 'NanoCpus': round(c['cpus'] * 1e9),
            'CapDrop': ['ALL'], 'CapAdd': [], 'SecurityOpt': ['no-new-privileges'],
            'Tmpfs': tmpfs_options(c), 'RestartPolicy': {'Name': 'no'},
            'Mounts': [{'Type': 'bind', 'Source': str(root), 'Target': '/workspace',
                        'BindOptions': {'NonRecursive': True}}]},
        'Config': {'User': f"{c['uid']}:{c['gid']}", 'Entrypoint': [plan.command[0]], 'Cmd': plan.command[1:],
            'WorkingDir': '/workspace', 'Labels': {'com.codepier.operation': name,
              'com.codepier.runtime': 'docker_restricted_v1'}, 'Healthcheck': {'Test': ['NONE']},
            'Env': [key + '=' + value for key, value in plan.environment.items()]},
        'Mounts': [{'Type': 'bind', 'Source': str(root), 'Destination': '/workspace',
                    'RW': True, 'Propagation': 'rprivate'}]}


@pytest.mark.parametrize('field,value', [('image', 'python:latest'), ('endpoint', 'tcp://remote:2375'),
    ('uid', 0), ('gid', 0), ('cpus', float('nan')), ('memory_mib', True), ('privileged', True)])
def test_unapproved_container_options_are_rejected(field, value):
    raw = {'engine': '/synthetic/docker', 'endpoint': 'unix:///synthetic/docker.sock', 'image': IMAGE,
           field: value}
    with pytest.raises(ValueError):
        validate_container(raw)


def test_command_uses_only_fixed_boundary_and_real_shared_project(container_plan):
    _, _, root, plan = container_plan
    c = plan.profile['container']
    args = create_arguments(c, root, plan, 'operation')
    assert '--pull=never' in args and '--read-only' in args
    assert args[args.index('--network') + 1] == 'none'
    assert args.count('--mount') == 1
    assert str(root) in args[args.index('--mount') + 1]
    assert 'bind-recursive=disabled' in args[args.index('--mount') + 1]
    assert not {'--privileged', '--volume', '-v', '--pid=host', '--network=host'} & set(args)
    assert args[-3:] == [IMAGE, '-c', 'exit 7']


@pytest.mark.parametrize('field,value', [('Privileged', True), ('ReadonlyRootfs', False),
    ('NetworkMode', 'host'), ('PidMode', 'host'), ('IpcMode', 'host'), ('Memory', 0),
    ('PidsLimit', 0), ('CapAdd', ['SYS_ADMIN']), ('SecurityOpt', ['seccomp=unconfined']),
    ('Devices', [{'PathOnHost': '/dev/sda'}]), ('CapDrop', []), ('Tmpfs', {})])
def test_inspection_rejects_weakened_actual_boundary(container_plan, field, value):
    _, _, root, plan = container_plan
    c = plan.profile['container']
    data = inspected(c, root, plan, 'operation')
    data['HostConfig'][field] = value
    with pytest.raises(DevError) as exc:
        verify_inspection(data, c, root, plan, 'operation', IMAGE)
    assert exc.value.code == 'RUNTIME_ISOLATION_UNVERIFIED'


def test_extra_volume_or_wrong_workload_is_not_isolation_proof(container_plan):
    _, _, root, plan = container_plan
    c = plan.profile['container']
    baseline = inspected(c, root, plan, 'operation')
    assert verify_inspection(baseline, c, root, plan, 'operation', IMAGE)['state'] == 'verified_for_operation'
    for modifier in ('extra-volume', 'changed-command', 'recursive-bind', 'wrong-image'):
        data = deepcopy(baseline)
        if modifier == 'extra-volume':
            data['Mounts'].append({'Type': 'volume', 'Destination': '/secrets'})
        elif modifier == 'changed-command':
            data['Config']['Cmd'] = ['-c', 'changed']
        elif modifier == 'recursive-bind':
            data['HostConfig']['Mounts'][0]['BindOptions']['NonRecursive'] = False
        else:
            data['Image'] = 'sha256:' + 'd' * 64
        with pytest.raises(DevError):
            verify_inspection(data, c, root, plan, 'operation', IMAGE)


def test_unsafe_mount_and_external_worktree_are_rejected(container_plan, tmp_path):
    _, _, root, _ = container_plan
    for bad in (Path('/'), Path.home()):
        with pytest.raises(DevError):
            validate_mount_root(bad)
    outside = tmp_path / 'git-metadata'
    outside.mkdir()
    (root / '.git').write_text('gitdir: ' + str(outside))
    with pytest.raises(DevError) as exc:
        validate_mount_root(root)
    assert exc.value.code == 'RUNTIME_WORKTREE_UNSUPPORTED'


def test_preparation_inspects_before_start_and_cleanup_uses_same_immutable_id(container_plan, monkeypatch):
    config, _, root, plan = container_plan
    driver = ContainerRuntime(config, root, plan, 'original-operation')
    calls = []
    monkeypatch.setattr('agent.container_runtime.readiness', lambda _: {'state': 'unknown'})
    def control(args, **kwargs):
        calls.append(args)
        if args[0] == 'info':
            return {'OSType': 'linux', 'SecurityOptions': ['name=seccomp,profile=builtin']}
        if args[:2] == ['image', 'inspect']:
            return [{'Id': IMAGE, 'Config': {}}]
        if args[0] == 'create':
            return CID
        if args[0] == 'inspect':
            return [inspected(driver.config, root, plan, driver.name)]
        if args[0] == 'rm':
            return CID
        raise AssertionError(args)
    monkeypatch.setattr(driver, '_control', control)
    command = driver.prepare()
    assert not any(row[0] == 'start' for row in calls)
    assert command[-3:] == ['start', '--attach', CID]
    assert driver.evidence['state'] == 'verified_for_operation'
    assert driver.cleanup() == {'state': 'removed', 'pending': False}
    driver.cleanup()
    assert sum(row[0] == 'rm' for row in calls) == 1
    with pytest.raises(DevError) as exc:
        ContainerRuntime(config, root, plan, 'original-operation')
    assert exc.value.code == 'RUNTIME_OPERATION_REQUIRES_RECOVERY'


def test_missing_runtime_does_not_create_or_fallback(container_plan, monkeypatch):
    config, _, root, plan = container_plan
    driver = ContainerRuntime(config, root, plan, 'missing-runtime')
    monkeypatch.setattr('agent.container_runtime.readiness', lambda _: {
        'state': 'unavailable', 'code': 'RUNTIME_ENGINE_UNAVAILABLE'})
    monkeypatch.setattr(driver, '_control', lambda *a, **k: pytest.fail('Unavailable runtime must not create a process'))
    with pytest.raises(DevError) as exc:
        driver.prepare()
    assert exc.value.code == 'RUNTIME_ENGINE_UNAVAILABLE'
    assert not (root / 'forbidden').exists()


def test_uncertain_creation_does_not_delete_by_name_or_replay(container_plan, monkeypatch):
    config, _, root, plan = container_plan
    driver = ContainerRuntime(config, root, plan, 'uncertain-create')
    driver.creation_attempted = True
    monkeypatch.setattr(driver, '_control', lambda *a, **k: pytest.fail('No guessed deletion'))
    assert driver.cleanup()['pending']
    assert json.loads(driver.receipt.read_text())['state'] == 'needs_review'


def test_project_mount_rejects_host_ipc_hardlinks_and_scan_budget(container_plan, tmp_path, monkeypatch):
    import socket
    _, _, root, _ = container_plan
    channel = socket.socket(socket.AF_UNIX)
    monkeypatch.chdir(root)
    try:
        channel.bind('host.sock')
        with pytest.raises(DevError):
            inspect_project_mount(root)
    finally:
        channel.close()
        (root / 'host.sock').unlink(missing_ok=True)
    source = tmp_path / 'private'
    source.write_text('private fixture')
    os.link(source, root / 'linked')
    with pytest.raises(DevError):
        inspect_project_mount(root)
    (root / 'linked').unlink()
    (root / 'one').write_text('ok')
    with pytest.raises(DevError) as exc:
        inspect_project_mount(root, max_entries=0)
    assert exc.value.code == 'RUNTIME_MOUNT_UNVERIFIED'


def test_workload_exit_and_cleanup_record_failure_remain_explicit(container_plan, monkeypatch):
    config, _, root, plan = container_plan
    driver = ContainerRuntime(config, root, plan, 'outcome')
    driver.identifier = CID
    state = {'Id': CID, 'State': {'Status': 'exited', 'Running': False, 'ExitCode': 7,
                                 'StartedAt': '2026-10-16T00:00:00Z', 'OOMKilled': False}}
    monkeypatch.setattr(driver, '_control', lambda args, **kw: [state] if args[0] == 'inspect' else CID)
    outcome = driver.outcome()
    assert outcome['workload_exit_code'] == 7 and outcome['state'] == 'confirmed'
    def broken_record(*args):
        raise OSError('fixture disk failure')
    monkeypatch.setattr(driver, '_record', broken_record)
    assert driver.cleanup()['pending']
    assert driver.workload_outcome == outcome


def test_real_container_qualification_is_an_explicit_mandatory_release_gate():
    # OS qualification is executed by CI's source job against the verified
    # archive, not silently skipped in the portable regression collection.
    from scripts.verify_project_runtime import CHECKS
    assert {'shared_read_write', 'timeout_enforced', 'cleanup_confirmed',
            'original_operation_replay_safe', 'network_denied'} <= CHECKS
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/ci.yml').read_text()
    assert 'Mandatory real project-container acceptance from public archive' in workflow
    assert 'scripts/verify_project_runtime.py' in workflow
    assert 'dist/runtime-acceptance.json' in workflow


@pytest.mark.asyncio
async def test_repeated_cancel_waits_for_late_creation_before_cleanup(container_plan, monkeypatch):
    import asyncio
    import threading

    config, _, root, plan = container_plan
    driver = ContainerRuntime(config, root, plan, 'cancel-during-create')
    entered, release, cleanup_entered = threading.Event(), threading.Event(), threading.Event()
    cleaned = []

    def preparing():
        driver.creation_attempted = True
        entered.set()
        assert release.wait(5), 'test did not release the bounded control call'
        driver.identifier = CID
        return ['synthetic-docker', 'start', '--attach', CID]

    def cleanup():
        cleaned.append(driver.identifier)
        cleanup_entered.set()
        return {'state': 'removed', 'pending': False}

    monkeypatch.setattr(driver, 'prepare', preparing)
    monkeypatch.setattr(driver, 'cleanup', cleanup)
    operation = asyncio.create_task(driver.prepare_async())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        # Give an erroneously abandoned create and its cleanup thread a real
        # bounded opportunity to run. sleep(0) alone can mask this thread race.
        assert not await asyncio.to_thread(cleanup_entered.wait, .5), 'cleanup began before create returned'
        assert cleaned == [], 'cleanup must not race ahead of the original create result'
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
    assert cleaned == [CID]


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['outcome', 'cleanup'])
async def test_repeated_cancel_keeps_original_outcome_and_cleanup_job(container_plan, monkeypatch, method):
    import asyncio
    import threading

    config, _, root, plan = container_plan
    driver = ContainerRuntime(config, root, plan, 'cancel-' + method)
    entered, release = threading.Event(), threading.Event()
    observed = []

    def control():
        entered.set()
        assert release.wait(5), 'test did not release the bounded control call'
        observed.append(method)
        return {'state': 'confirmed'}

    monkeypatch.setattr(driver, method, control)
    operation = asyncio.create_task(getattr(driver, method + '_async')())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not operation.done(), 'cancellation cannot abandon an active control call'
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
    assert observed == [method]


def test_unknown_create_preserves_original_receipt_and_rejects_replay(container_plan, monkeypatch):
    config, _, root, plan = container_plan
    driver = ContainerRuntime(config, root, plan, 'lost-create-response')
    calls = []
    monkeypatch.setattr('agent.container_runtime.readiness', lambda _: {'state': 'unknown'})

    def control(args, **kwargs):
        calls.append(args)
        if args[0] == 'info':
            return {'OSType': 'linux', 'SecurityOptions': ['name=seccomp,profile=builtin']}
        if args[:2] == ['image', 'inspect']:
            return [{'Id': IMAGE, 'Config': {}}]
        if args[0] == 'create':
            raise DevError('RUNTIME_CONTROL_UNCERTAIN', 'fixture lost create response', 409)
        pytest.fail('No guessed inspection, deletion or native fallback after unknown create')

    monkeypatch.setattr(driver, '_control', control)
    with pytest.raises(DevError) as exc:
        driver.prepare()
    assert exc.value.code == 'RUNTIME_CREATION_UNCERTAIN'
    receipt = driver.receipt.read_bytes()
    saved = json.loads(receipt)
    assert saved['state'] == 'needs_review' and saved['container_id'] is None
    assert saved['cleanup']['pending'] is True
    assert saved['runtime_ref'] == plan.runtime['runtime_ref']
    assert saved['revision'] == plan.runtime['revision']
    with pytest.raises(DevError) as repeated:
        ContainerRuntime(config, root, plan, 'lost-create-response')
    assert repeated.value.code == 'RUNTIME_OPERATION_REQUIRES_RECOVERY'
    assert driver.receipt.read_bytes() == receipt
    assert [row[0] for row in calls] == ['info', 'image', 'create']


@pytest.mark.parametrize('section,key,value', [
    ('State', 'Running', None), ('State', 'Running', 0), ('State', 'Running', ''),
    ('HostConfig', 'Privileged', 0), ('HostConfig', 'ReadonlyRootfs', 1),
    ('HostConfig', 'RestartPolicy', None), ('HostConfig', 'RestartPolicy', []),
    ('HostConfig', 'CapDrop', [['ALL']]), ('HostConfig', 'SecurityOpt', [None]),
    ('HostConfig', 'SecurityOpt', [{'no-new-privileges': True}]),
])
def test_inspection_rejects_malformed_state_and_security_fields(container_plan, section, key, value):
    _, _, root, plan = container_plan
    data = inspected(plan.profile['container'], root, plan, 'operation')
    data[section][key] = value
    with pytest.raises(DevError) as exc:
        verify_inspection(data, plan.profile['container'], root, plan, 'operation', IMAGE)
    assert exc.value.code == 'RUNTIME_ISOLATION_UNVERIFIED'


@pytest.mark.parametrize('running', [None, 0, ''])
def test_unknown_running_state_never_confirms_workload_success(container_plan, monkeypatch, running):
    config, _, root, plan = container_plan
    driver = ContainerRuntime(config, root, plan, 'missing-running-state')
    driver.identifier = CID
    state = {'Id': CID, 'State': {'Status': 'exited', 'Running': running,
        'ExitCode': 0, 'StartedAt': '2026-10-19T00:00:00Z'}}
    monkeypatch.setattr(driver, '_control', lambda *args, **kwargs: [state])
    result = driver.outcome()
    assert result['state'] == 'unconfirmed'
    assert result['workload_exit_code'] is None


@pytest.mark.parametrize('stage,payload,code', [
    ('info', [], 'RUNTIME_ENGINE_UNSUPPORTED'),
    ('info', {'OSType': 'linux', 'SecurityOptions': None}, 'RUNTIME_ENGINE_UNSUPPORTED'),
    ('info', {'OSType': 'linux', 'SecurityOptions': [None]}, 'RUNTIME_ENGINE_UNSUPPORTED'),
    ('info', {'OSType': 'linux', 'SecurityOptions': ['name=seccomp-unconfined']},
        'RUNTIME_ENGINE_UNSUPPORTED'),
    ('image', [None], 'RUNTIME_IMAGE_UNSUPPORTED'),
    ('image', [{'Id': IMAGE, 'Config': None}], 'RUNTIME_IMAGE_UNSUPPORTED'),
    ('image', [{'Id': None, 'Config': {}}], 'RUNTIME_IMAGE_UNSUPPORTED'),
])
def test_malformed_backend_discovery_never_starts_project_code(container_plan, monkeypatch, stage, payload, code):
    config, _, root, plan = container_plan
    driver = ContainerRuntime(config, root, plan, 'malformed-discovery')
    calls = []
    monkeypatch.setattr('agent.container_runtime.readiness', lambda _: {'state': 'unknown'})

    def control(args, **kwargs):
        calls.append(args)
        if args[0] == 'info':
            return payload if stage == 'info' else {'OSType': 'linux',
                'SecurityOptions': ['name=seccomp,profile=builtin']}
        if args[:2] == ['image', 'inspect']:
            return payload
        pytest.fail('Invalid backend discovery must fail before create or start')

    monkeypatch.setattr(driver, '_control', control)
    with pytest.raises(DevError) as exc:
        driver.prepare()
    assert exc.value.code == code
    assert not driver.creation_attempted
    assert not driver.receipt.exists()
    assert [args[0] for args in calls] == (['info'] if stage == 'info' else ['info', 'image'])


@pytest.mark.parametrize('change', ['restart-name', 'mount-type', 'mount-destination',
    'env-item', 'env-no-separator', 'env-empty-name', 'env-duplicate'])
def test_ambiguous_inspection_never_proves_isolation(container_plan, change):
    _, _, root, plan = container_plan
    data = inspected(plan.profile['container'], root, plan, 'operation')
    if change == 'restart-name':
        data['HostConfig']['RestartPolicy']['Name'] = []
    elif change == 'mount-type':
        data['Mounts'][0]['Type'] = []
    elif change == 'mount-destination':
        data['Mounts'].append({'Type': 'tmpfs', 'Destination': {}})
    elif change == 'env-item':
        data['Config']['Env'].append(None)
    elif change == 'env-no-separator':
        data['Config']['Env'].append('HOME')
    elif change == 'env-empty-name':
        data['Config']['Env'].append('=unexpected')
    else:
        # Last-value-wins parsing can hide the earlier value seen by a workload.
        data['Config']['Env'].insert(0, 'HOME=/unexpected')
    with pytest.raises(DevError) as exc:
        verify_inspection(data, plan.profile['container'], root, plan, 'operation', IMAGE)
    assert exc.value.code == 'RUNTIME_ISOLATION_UNVERIFIED'


@pytest.mark.parametrize('payload', [[None], [[]], ['invalid']])
@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_malformed_created_inspection_retains_exact_cleanup_identity(
        container_plan, monkeypatch, payload, cleanup_fails):
    config, _, root, plan = container_plan
    driver = ContainerRuntime(config, root, plan, 'malformed-created-inspection')
    calls = []
    monkeypatch.setattr('agent.container_runtime.readiness', lambda _: {'state': 'unknown'})

    def control(args, **kwargs):
        calls.append(args)
        if args[0] == 'info':
            return {'OSType': 'linux', 'SecurityOptions': ['name=seccomp,profile=builtin']}
        if args[:2] == ['image', 'inspect']:
            return [{'Id': IMAGE, 'Config': {}}]
        if args[0] == 'create':
            return CID
        if args[0] == 'inspect':
            return payload
        if args[0] == 'rm':
            if cleanup_fails:
                raise DevError('RUNTIME_ENGINE_FAILED', 'fixture cleanup failure', 409)
            return CID
        pytest.fail('Unverified container must never start')

    monkeypatch.setattr(driver, '_control', control)
    with pytest.raises(DevError) as exc:
        driver.prepare()
    assert exc.value.code == ('RUNTIME_CREATION_UNCERTAIN' if cleanup_fails
                              else 'RUNTIME_ISOLATION_UNVERIFIED')
    assert driver.evidence is None
    assert [args for args in calls if args[0] == 'rm'] == [['rm', '--force', CID]]
    receipt = json.loads(driver.receipt.read_text())
    assert receipt['container_id'] == CID
    assert receipt['cleanup']['pending'] is cleanup_fails
    with pytest.raises(DevError) as repeated:
        ContainerRuntime(config, root, plan, 'malformed-created-inspection')
    assert repeated.value.code == 'RUNTIME_OPERATION_REQUIRES_RECOVERY'
