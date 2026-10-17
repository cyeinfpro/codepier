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
