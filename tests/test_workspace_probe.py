import asyncio
import os
import sys

import pytest

from agent import workspace_probe
from shared.contracts import TOOLS
from shared.tool_protocol import wire_version
from shared.util import DevError
from agent.runner import Agent
from shared.crypto import token
from shared.util import atomic_json


@pytest.fixture
def local_agent(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    path = tmp_path / 'config.json'
    atomic_json(path, {'hub_url': 'http://127.0.0.1:9', 'device_id': 'fixture',
                      'secret': token(), 'state_dir': str(tmp_path / 'state'),
                      'allowed_roots': [{'path': str(root), 'writable': True,
                                         'allow_tasks': True}], 'tasks': {}})
    agent = Agent(path)
    yield agent, root
    agent.journal.db.close()
    agent.instance_lock.close()


@pytest.mark.asyncio
async def test_discovery_matches_in_process_and_keeps_authorization(local_agent):
    agent, root = local_agent
    (root / 'AGENTS.md').write_text('Project instructions\n')
    project = {'id': 'fixture', 'alias': 'fixture', 'root': str(root),
               'mode': 'write', 'allow_tasks': True, '_coding_owner': 'grant:test'}
    args = TOOLS['open_workspace'].model.model_validate({'project': 'fixture'}).model_dump()
    expected = agent.engine.call('open_workspace', project, args)
    result = await agent.execute('probe', 'open_workspace', project, args)
    assert result['context_id'] == expected['context_id']
    assert result['context'] == expected['context']
    with pytest.raises(DevError, match='授权'):
        await agent.execute('outside', 'open_workspace', {**project, 'root': str(root.parent)}, args)


@pytest.mark.asyncio
@pytest.mark.parametrize('tool,args', [('open_workspace', {}), ('read', {'path': 'README.md'})])
@pytest.mark.parametrize('blocked_phase', ['admission', 'root', 'execute'])
async def test_blocked_discovery_is_reaped_and_releases_slots(
        local_agent, monkeypatch, tmp_path, tool, args, blocked_phase):
    agent, root = local_agent
    (root / 'README.md').write_text('Readable after recovery')
    marker = tmp_path / 'pid'
    original_probe = workspace_probe.probe
    async def blocked(engine, tool, project, args, *, phase='execute', expected_root=None, worker=None):
        if phase == blocked_phase:
            code = ('import os,time; from pathlib import Path; '
                    f'Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)')
            spawn = asyncio.create_subprocess_exec
            async def ready_spawn(*command, **kwargs):
                process = await spawn(*command, **kwargs)
                try:
                    async with asyncio.timeout(5):
                        while not marker.exists():
                            await asyncio.sleep(.01)
                    return process
                except BaseException:
                    await workspace_probe._reap(process)
                    raise
            # Begin the actual run_worker communication timeout only after the
            # child is ready, independent of interpreter/coverage startup cost.
            with monkeypatch.context() as startup_patch:
                startup_patch.setattr(asyncio, 'create_subprocess_exec', ready_spawn)
                return await workspace_probe.run_worker(
                    [sys.executable, '-c', code], b'', cwd=str(tmp_path), timeout=.25)
        return await original_probe(engine, tool, project, args, phase=phase, expected_root=expected_root, worker=worker)
    project = {'id': 'fixture', 'alias': 'fixture', 'root': str(root),
               'mode': 'write', '_coding_owner': 'grant:test'}
    with monkeypatch.context() as patch:
        patch.setattr(workspace_probe, 'probe', blocked)
        # Root/path IO must never fall back onto the event loop before execute.
        patch.setattr(agent.engine, 'root', lambda *a, **k: pytest.fail('parent root IO'))
        patch.setattr(agent.engine, 'path', lambda *a, **k: pytest.fail('parent path IO'))
        task = asyncio.create_task(agent.handle({'id': 'blocked', 'tool': tool,
            'tool_contract_version': wire_version(tool),
            'project': project, 'args': {'project': 'fixture', **args}}))
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(.01)
        assert await asyncio.wait_for(asyncio.to_thread(lambda: 'responsive'), .3) == 'responsive'
        await asyncio.wait_for(task, 5)
    result = agent.journal.status('blocked')['result']
    assert result['error']['code'] == 'PROJECT_DISCOVERY_TIMEOUT'
    assert not agent._active_slots
    assert not agent.resources.active and not agent.resources.waiting
    assert agent.scheduler.snapshot()['running'] == 0
    if os.name != 'nt':
        with pytest.raises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)
    await agent.handle({'id': 'recovered', 'tool': tool,
        'tool_contract_version': wire_version(tool),
        'project': project, 'args': {'project': 'fixture', **args}})
    assert agent.journal.status('recovered')['result']['ok']


@pytest.mark.asyncio
async def test_cancellation_reaps_worker(tmp_path):
    marker = tmp_path / 'pid'
    command = [sys.executable, '-c', 'import os,time; from pathlib import Path; '
               f'Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)']
    task = asyncio.create_task(workspace_probe.run_worker(command, b'', cwd=str(tmp_path)))
    async with asyncio.timeout(4):
        while not marker.exists():
            await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    if os.name != 'nt':
        with pytest.raises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('output', ['not json', '[]', '{"ok":true}', '{"ok":false,"error":{}}'])
async def test_invalid_worker_result_is_not_success(tmp_path, output):
    with pytest.raises(DevError) as error:
        await workspace_probe.run_worker(
            [sys.executable, '-c', f'print({output!r})'], b'', cwd=str(tmp_path))
    assert error.value.code == 'PROJECT_DISCOVERY_FAILED'


@pytest.mark.asyncio
async def test_worker_nonzero_exit_is_not_success(tmp_path):
    with pytest.raises(DevError) as error:
        await workspace_probe.run_worker(
            [sys.executable, '-c', 'raise SystemExit(7)'], b'', cwd=str(tmp_path))
    assert error.value.code == 'PROJECT_DISCOVERY_FAILED'


@pytest.mark.asyncio
async def test_cancel_during_spawn_still_reaps_child(tmp_path, monkeypatch):
    created = asyncio.Event()
    release = asyncio.Event()
    children = []
    original = asyncio.create_subprocess_exec
    async def delayed(*args, **kwargs):
        process = await original(*args, **kwargs)
        children.append(process)
        created.set()
        await release.wait()
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed)
    task = asyncio.create_task(workspace_probe.run_worker(
        [sys.executable, '-c', 'import time; time.sleep(60)'], b'', cwd=str(tmp_path)))
    await asyncio.wait_for(created.wait(), 4)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 4)
    assert children[0].returncode is not None


@pytest.mark.asyncio
async def test_cancellation_during_reap_is_not_success(tmp_path, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    original = workspace_probe._reap
    async def delayed(process):
        entered.set()
        await release.wait()
        await original(process)
    monkeypatch.setattr(workspace_probe, '_reap', delayed)
    task = asyncio.create_task(workspace_probe.run_worker(
        [sys.executable, '-c', 'print(\'{"ok":true,"data":1}\')'], b'', cwd=str(tmp_path)))
    await asyncio.wait_for(entered.wait(), 4)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 4)


@pytest.mark.asyncio
async def test_authorization_revoked_while_queued(local_agent, monkeypatch):
    agent, root = local_agent
    spawn = asyncio.create_subprocess_exec
    processes = []
    async def counted(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', counted)
    (root / 'README.md').write_text('read me')
    from agent.resource_queue import Claim, canonical_path
    project = {'id': 'fixture', 'root': str(root), 'alias': 'fixture', 'mode': 'write'}
    async with agent.resources.slot('writer', [Claim('agent', 'path', canonical_path(root), True)], lambda _: None):
        task = asyncio.create_task(agent.handle({
            'id': 'revoked', 'tool': 'read', 'tool_contract_version': wire_version('read'),
            'project': project, 'args': {'project': 'fixture', 'path': 'README.md'}}))
        async with asyncio.timeout(5):
            while not agent.resources.waiting:
                assert not task.done()
                await asyncio.sleep(.01)
        assert len(processes) == 1 and processes[0].returncode == 0
        assert not agent._read_workers
        agent.config['allowed_roots'] = []
    await asyncio.wait_for(task, 5)
    result = agent.journal.status('revoked')['result']
    assert result['error']['code'] == 'ROOT_NOT_ALLOWED'
    assert not agent.resources.active and not agent.resources.waiting
    assert agent.scheduler.snapshot()['running'] == 0


@pytest.mark.asyncio
async def test_root_change_is_rejected(local_agent, tmp_path):
    agent, root = local_agent
    from agent.resource_queue import Claim, canonical_path
    second = tmp_path / 'second'
    second.mkdir()
    agent.config['allowed_roots'].append({'path': str(second), 'writable': True})
    project = {'id': 'fixture', 'root': str(root), 'alias': 'fixture', 'mode': 'write'}
    async with agent.resources.slot('writer', [Claim('agent', 'path', canonical_path(root), True)], lambda _: None):
        task = asyncio.create_task(agent.handle({
            'id': 'remapped', 'tool': 'open_workspace', 'tool_contract_version': wire_version('open_workspace'),
            'project': project, 'args': {'project': 'fixture'}}))
        async with asyncio.timeout(5):
            while not agent.resources.waiting:
                assert not task.done()
                await asyncio.sleep(.01)
        # Change the admitted operation's mapping while the real resource
        # queue holds it. Both roots remain authorized; actual child-process
        # root resolution must reject the change rather than read the new root.
        agent.integration_projects['remapped']['root'] = str(second)
    await asyncio.wait_for(task, 5)
    assert agent.journal.status('remapped')['result']['error']['code'] == 'ROOT_CHANGED'
    assert not agent._active_slots and not agent.resources.active
    assert agent.scheduler.snapshot()['running'] == 0


@pytest.mark.asyncio
async def test_probe_preserves_error_status_and_blocks_non_read(local_agent):
    agent, root = local_agent
    project = {'id': 'fixture', 'root': str(root), 'alias': 'fixture', 'mode': 'write'}
    with pytest.raises(DevError) as error:
        await workspace_probe.probe(agent.engine, 'open_workspace',
                                    {**project, 'root': str(root.parent)}, {})
    assert error.value.code == 'ROOT_NOT_ALLOWED'
    assert error.value.status == 403
    for tool, args in [('fs_write', {}), ('open_workspace', {'capture_baseline': True})]:
        with pytest.raises(DevError) as error:
            await workspace_probe.probe(agent.engine, tool, project, args)
        assert error.value.code == 'INVALID_REQUEST'


@pytest.mark.asyncio
@pytest.mark.parametrize('tool,extra', [
    ('project_context', {}),
    ('fs_read', {'path': 'README.md'}),
    ('fs_read_many', {'paths': ['README.md', 'absent.md']}),
    ('fs_tree', {'path': '.'}),
    ('fs_search', {'path': '.', 'query': 'hello'}),
    ('read', {'path': 'README.md'}),
])
async def test_read_results_and_claims_match_existing_rules(local_agent, tool, extra):
    from dataclasses import asdict
    from agent.core_files import CoreFiles
    from agent.resource_queue import Claim, canonical_path, claims_for, PATH_READ_TOOLS
    agent, root = local_agent
    (root / 'README.md').write_text('hello world\n')
    project = {'id': 'fixture', 'root': str(root), 'alias': 'fixture', 'mode': 'write'}
    args = TOOLS[tool].model.model_validate({'project': 'fixture', **extra}).model_dump()
    expected = (CoreFiles(agent.engine).call(tool, project, args) if tool == 'read'
                else agent.engine.call(tool, project, args))
    result = await workspace_probe.probe(agent.engine, tool, project, args)
    assert result == expected
    expected_claims = (claims_for(agent.engine, tool, project, args, root)
                       if tool == 'read' or tool in PATH_READ_TOOLS else
                       [Claim('agent', 'path', canonical_path(root), False)])
    admission = await workspace_probe.probe(agent.engine, tool, project, args,
                                            phase='claims', expected_root=root)
    assert admission['claims'] == [asdict(claim) for claim in expected_claims]
    assert admission['project_key'] == 'fixture'


@pytest.mark.asyncio
async def test_cancelled_handle_releases_slots_and_recovers(local_agent, monkeypatch, tmp_path):
    agent, root = local_agent
    original = workspace_probe.probe
    marker = tmp_path / 'cancel-pid'
    async def blocked(engine, tool, project, args, *, phase='execute', expected_root=None, worker=None):
        if phase == 'execute':
            code = ('import os,time; from pathlib import Path; '
                    f'Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)')
            return await workspace_probe.run_worker(
                [sys.executable, '-c', code], b'', cwd=str(tmp_path))
        return await original(engine, tool, project, args, phase=phase, expected_root=expected_root, worker=worker)
    project = {'id': 'fixture', 'root': str(root), 'alias': 'fixture', 'mode': 'write',
               '_coding_owner': 'grant:test'}
    request = {'id': 'cancelled', 'tool': 'open_workspace',
               'tool_contract_version': wire_version('open_workspace'),
               'project': project, 'args': {'project': 'fixture'}}
    with monkeypatch.context() as patch:
        patch.setattr(workspace_probe, 'probe', blocked)
        task = asyncio.create_task(agent.handle(request))
        async with asyncio.timeout(5):
            while not marker.exists():
                assert not task.done()
                await asyncio.sleep(.01)
        task.cancel()
        await asyncio.wait_for(task, 4)
    assert agent.journal.status('cancelled')['result']['error']['code'] == 'INTERRUPTED'
    assert not agent.resources.active and not agent._active_slots
    assert agent.scheduler.snapshot()['running'] == 0
    await agent.handle({**request, 'id': 'after-cancel'})
    assert agent.journal.status('after-cancel')['result']['ok']


@pytest.mark.asyncio
async def test_preflight_burst_is_bounded_and_other_project_has_reserved_slot(local_agent, monkeypatch):
    agent, root = local_agent
    release = asyncio.Event()
    gamma_entered = asyncio.Event()
    entered = []
    peak = 0
    async def blocked(engine, tool, project, args):
        nonlocal peak
        entered.append(project['id'])
        peak = max(peak, len(agent.scheduler.active))
        if project['id'] == 'gamma':
            gamma_entered.set()
        await release.wait()
        return root, {}
    monkeypatch.setattr(workspace_probe, 'root', blocked)
    alpha = {'id': 'alpha', 'root': str(root)}
    tasks = [asyncio.create_task(agent._operation_root(
        'read', alpha, {}, preflight_id=f'alpha-{i}')) for i in range(128)]
    try:
        limit = agent.scheduler.lane_limit('read')
        async with asyncio.timeout(5):
            while len(entered) < limit - 1:
                await asyncio.sleep(.01)
        assert len(entered) == limit - 1
        gamma = asyncio.create_task(agent._operation_root(
            'read', {'id': 'gamma', 'root': str(root)}, {}, preflight_id='gamma'))
        tasks.append(gamma)
        await asyncio.wait_for(gamma_entered.wait(), 2)
        assert peak <= limit
        assert entered.count('alpha') == limit - 1
        # Cancelling a queued preflight removes its scheduler ticket, without
        # creating a worker or consuming a filesystem resource.
        tasks[-2].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[-2]
        assert not agent.resources.active
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        assert entered.count('alpha') == 127
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not agent.scheduler.active and not agent.scheduler.waiting


@pytest.mark.asyncio
async def test_unresolved_legacy_roots_share_conservative_preflight_quota(local_agent, monkeypatch):
    agent, root = local_agent
    keys = []
    async def inspect(engine, tool, project, args):
        keys.append(next(iter(agent.scheduler.active.values())).project)
        return root, {}
    monkeypatch.setattr(workspace_probe, 'root', inspect)
    for index, project in enumerate([
            {'root': '/unresolved/one', 'alias': 'one'},
            {'root': '/unresolved/two', 'alias': 'two', 'id': ''},
            {'root': '/unresolved/three', 'id': 'bound-project'}]):
        await agent._operation_root('read', project, {}, preflight_id=str(index))
    assert keys == ['__unresolved_read_preflight__', '__unresolved_read_preflight__', 'bound-project']
    assert not agent.scheduler.active


@pytest.mark.asyncio
async def test_read_rechecks_do_not_reenter_execution_slot(local_agent, monkeypatch):
    agent, root = local_agent
    (root / 'README.md').write_text('read me')
    original = workspace_probe.probe
    spawn = asyncio.create_subprocess_exec
    processes = []
    async def counted(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', counted)
    observed = []
    async def inspect(engine, tool, project, args, *, phase='execute', expected_root=None, worker=None):
        active = list(agent.scheduler.active.values())
        observed.append((phase, [(entry.operation_id, entry.lane) for entry in active]))
        return await original(engine, tool, project, args, phase=phase, expected_root=expected_root, worker=worker)
    monkeypatch.setattr(workspace_probe, 'probe', inspect)
    await asyncio.wait_for(agent.handle({
        'id': 'single-slot', 'tool': 'read', 'tool_contract_version': wire_version('read'),
        'project': {'id': 'fixture', 'root': str(root), 'alias': 'fixture', 'mode': 'write'},
        'args': {'project': 'fixture', 'path': 'README.md'}}), 5)
    assert agent.journal.status('single-slot')['result']['ok']
    assert [phase for phase, _ in observed] == ['admission', 'root', 'root', 'execute']
    assert all(active == [('single-slot', 'read')] for _, active in observed)
    assert len(processes) == 2
    assert all(process.returncode == 0 for process in processes)
    assert not agent._read_workers
    assert not agent.scheduler.active and not agent.scheduler.waiting


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['deadline', 'cancel'])
async def test_first_preflight_wait_expires_or_cancels_without_spawning(
        local_agent, monkeypatch, ending):
    import contextlib
    import time
    agent, root = local_agent
    async def forbidden(*args, **kwargs):
        pytest.fail('An unadmitted preflight must not spawn a root worker')
    monkeypatch.setattr(workspace_probe, 'probe', forbidden)
    reserved = agent.scheduler.lane_limit('read') - 1
    async with contextlib.AsyncExitStack() as stack:
        for index in range(reserved):
            await stack.enter_async_context(agent.scheduler.slot(
                f'holder-{index}', 'fixture', 'read'))
        task = asyncio.create_task(agent.handle({
            'id': 'preflight-ended', 'tool': 'read',
            'tool_contract_version': wire_version('read'),
            'not_after': time.time() + (0.15 if ending == 'deadline' else 30),
            'project': {'id': 'fixture', 'root': str(root), 'mode': 'write'},
            'args': {'project': 'fixture', 'path': 'README.md'}}))
        async with asyncio.timeout(3):
            while not agent.scheduler.waiting:
                assert not task.done()
                await asyncio.sleep(.01)
        if ending == 'cancel':
            task.cancel()
        await asyncio.wait_for(task, 3)
        expected = 'QUEUE_EXPIRED' if ending == 'deadline' else 'INTERRUPTED'
        assert agent.journal.status('preflight-ended')['result']['error']['code'] == expected
        assert not agent.scheduler.waiting
        assert len(agent.scheduler.active) == reserved
        assert not agent.resources.active and not agent.resources.waiting
    assert not agent.scheduler.active


@pytest.mark.asyncio
@pytest.mark.parametrize('change_at,change', [(1, 'revoke'), (1, 'remap'), (2, 'revoke')])
async def test_session_rechecks_current_authority_and_mapping(local_agent, tmp_path, change_at, change):
    agent, root = local_agent
    (root / 'README.md').write_text('authorized content')
    project = {'id': 'fixture', 'root': str(root), 'mode': 'write'}
    args = TOOLS['read'].model.model_validate({'project': 'fixture', 'path': 'README.md'}).model_dump()
    async with workspace_probe.ReadWorker() as worker:
        for _ in range(change_at):
            await workspace_probe.probe(agent.engine, 'read', project, args, phase='root',
                                        expected_root=root, worker=worker)
        if change == 'revoke':
            agent.config['allowed_roots'] = []
        else:
            second = tmp_path / 'second-session'
            second.mkdir()
            agent.config['allowed_roots'].append({'path': str(second), 'writable': True})
            project['root'] = str(second)
        with pytest.raises(DevError) as error:
            await workspace_probe.probe(agent.engine, 'read', project, args,
                phase='execute' if change_at == 2 else 'root', expected_root=root, worker=worker)
        assert error.value.code == ('ROOT_NOT_ALLOWED' if change == 'revoke' else 'ROOT_CHANGED')
    assert worker.process.returncode is not None


@pytest.mark.asyncio
async def test_session_execute_once_and_parent_rejects_duplicate(local_agent):
    agent, root = local_agent
    (root / 'README.md').write_text('once')
    project = {'id': 'fixture', 'root': str(root), 'mode': 'write'}
    args = TOOLS['read'].model.model_validate({'project': 'fixture', 'path': 'README.md'}).model_dump()
    async with workspace_probe.ReadWorker() as worker:
        for _ in range(2):
            await workspace_probe.probe(agent.engine, 'read', project, args, phase='root',
                                        expected_root=root, worker=worker)
        result = await workspace_probe.probe(agent.engine, 'read', project, args,
                                            expected_root=root, worker=worker)
        assert result['content'] == 'once'
        assert worker.process.returncode == 0
        with pytest.raises(DevError) as error:
            await workspace_probe.probe(agent.engine, 'read', project, args,
                                        expected_root=root, worker=worker)
        assert error.value.code == 'INVALID_REQUEST'


@pytest.mark.asyncio
@pytest.mark.parametrize('phases', [('execute',), ('root', 'execute'),
                                  ('root', 'root', 'execute', 'execute')])
async def test_worker_enforces_protocol_even_without_parent(local_agent, phases):
    import json
    agent, root = local_agent
    (root / 'README.md').write_text('once')
    args = TOOLS['read'].model.model_validate({'project': 'fixture', 'path': 'README.md'}).model_dump()
    request = {'config': agent.config, 'config_path': str(agent.config_path),
               'state_dir': str(agent.journal.directory), 'tool': 'read',
               'project': {'id': 'fixture', 'root': str(root)}, 'args': args,
               'expected_root': str(root)}
    base, command = workspace_probe.worker_command('session_main')
    process = await asyncio.create_subprocess_exec(*command, cwd=base,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    try:
        frames = b''.join((json.dumps({**request, 'phase': phase}) + chr(10)).encode() for phase in phases)
        output, _ = await asyncio.wait_for(process.communicate(frames), 5)
        results = [json.loads(line) for line in output.splitlines()]
        if len(phases) < 3:
            assert results[-1]['error']['code'] == 'INVALID_REQUEST'
        else:
            assert len(results) == 3
            assert results[-1]['data']['content'] == 'once'
        assert process.returncode == 0
    finally:
        await workspace_probe._reap(process)


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['cancel', 'timeout'])
async def test_session_blocked_read_reaps_process(local_agent, monkeypatch, tmp_path, ending):
    original = workspace_probe.worker_command
    marker = tmp_path / 'session-ready'
    code = (
        "import sys,json,time; from pathlib import Path; "
        "json.loads(sys.stdin.readline()); print(json.dumps({'ok':True,'data':{}}),flush=True); "
        "json.loads(sys.stdin.readline()); print(json.dumps({'ok':True,'data':{}}),flush=True); "
        f"json.loads(sys.stdin.readline()); Path({str(marker)!r}).write_text('ready'); time.sleep(60)")
    def synthetic(entry='main'):
        if entry == 'session_main':
            return str(tmp_path), [sys.executable, '-c', code]
        return original(entry)
    monkeypatch.setattr(workspace_probe, 'worker_command', synthetic)
    async with workspace_probe.ReadWorker() as worker:
        await worker.request({'phase': 'root'})
        await worker.request({'phase': 'root'})
        # Both prior frames prove interpreter startup has completed.
        if ending == 'timeout':
            monkeypatch.setattr(workspace_probe, 'PROBE_TIMEOUT', .25)
        pending = asyncio.create_task(worker.request({'phase': 'execute'}))
        async with asyncio.timeout(3):
            while not marker.exists():
                assert not pending.done()
                await asyncio.sleep(.01)
        if ending == 'cancel':
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            with pytest.raises(DevError) as error:
                await pending
            assert error.value.code == 'PROJECT_DISCOVERY_TIMEOUT'
    assert worker.process.returncode is not None
