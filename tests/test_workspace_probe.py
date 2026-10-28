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
@pytest.mark.parametrize('blocked_phase', ['root', 'claims', 'execute'])
async def test_blocked_discovery_is_reaped_and_releases_slots(
        local_agent, monkeypatch, tmp_path, tool, args, blocked_phase):
    agent, root = local_agent
    (root / 'README.md').write_text('Readable after recovery')
    marker = tmp_path / 'pid'
    original_probe = workspace_probe.probe
    async def blocked(engine, tool, project, args, *, phase='execute', expected_root=None):
        if phase == blocked_phase:
            code = ('import os,time; from pathlib import Path; '
                    f'Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)')
            return await workspace_probe.run_worker(
                [sys.executable, '-c', code], b'', cwd=str(tmp_path), timeout=.25)
        return await original_probe(engine, tool, project, args, phase=phase, expected_root=expected_root)
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
async def test_authorization_revoked_while_queued(local_agent):
    agent, root = local_agent
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
    async def blocked(engine, tool, project, args, *, phase='execute', expected_root=None):
        if phase == 'execute':
            code = ('import os,time; from pathlib import Path; '
                    f'Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)')
            return await workspace_probe.run_worker(
                [sys.executable, '-c', code], b'', cwd=str(tmp_path))
        return await original(engine, tool, project, args, phase=phase, expected_root=expected_root)
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
