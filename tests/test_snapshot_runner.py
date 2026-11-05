"""End-to-end admitted exec output paths and snapshot lifecycle."""
import asyncio
import json
from pathlib import Path
import shutil
import threading
import time

import pytest

from agent.project_runtime import validate_runtimes
from agent.resource_queue import Claim, canonical_path
from agent.snapshots import SnapshotManager
from shared.util import DevError
from tests.test_shell import shell_agent, shell_request  # noqa: F401


def request(root, command="printf ok", **options):
    req = shell_request(root, command, **options)
    req['tool'] = 'exec'
    from shared.tool_protocol import wire_version
    req['tool_contract_version'] = wire_version('exec')
    return req


def clean(agent):
    agent.config['project_runtimes'] = validate_runtimes({'fixture': {'mode': 'project_clean_v1'}})


def result(agent, req):
    return agent.journal.status(req['id'])['result']


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["native", "clean"])
async def test_local_run_dir_overrides_caller_env(shell_agent, mode):
    agent, root = shell_agent
    if mode == "clean":
        clean(agent)
    req = request(root, 'printf report > "$CODEPIER_RUN_DIR/report.txt"; printf "%s" "$CODEPIER_RUN_DIR"',
                  env={"CODEPIER_RUN_DIR": "/not/allowed"})
    await agent.handle(req)
    outcome = result(agent, req)
    assert outcome['ok'], outcome
    data = outcome['data']
    path = root / '.codepier/runs' / req['id']
    assert data['run_directory'] == '.codepier/runs/' + req['id']
    assert data['output'] == str(path)
    assert (path / 'report.txt').read_text() == 'report'
    assert json.loads((path / '.codepier-run.json').read_text())['finished']
    assert not agent._execution_run_dirs


@pytest.mark.asyncio
async def test_snapshot_mutations_and_artifacts_are_isolated(shell_agent, monkeypatch):
    agent, root = shell_agent
    clean(agent)
    monkeypatch.setattr('agent.snapshots._clone_file', shutil.copy2)
    (root / 'source.txt').write_text('original')
    req = request(root, 'printf changed > source.txt; mkdir reports; printf report > reports/test.txt',
                  isolation='snapshot', snapshot_artifacts=['reports'])
    await agent.handle(req)
    outcome = result(agent, req)
    assert outcome['ok'], outcome
    data = outcome['data']
    assert data['exit_code'] == 0
    assert (root / 'source.txt').read_text() == 'original'
    assert not (root / 'reports').exists()
    assert (root / data['run_directory'] / 'reports/test.txt').read_text() == 'report'
    assert data['snapshot_artifacts'] == ['reports']
    assert not (agent.state_dir / 'snapshots' / req['id']).exists()
    assert not agent._execution_snapshots


@pytest.mark.asyncio
async def test_failed_snapshot_is_retained(shell_agent, monkeypatch):
    agent, root = shell_agent
    clean(agent)
    monkeypatch.setattr('agent.snapshots._clone_file', shutil.copy2)
    (root / 'source').write_text('x')
    req = request(root, 'exit 7', isolation='snapshot')
    await agent.handle(req)
    outcome = result(agent, req)
    assert outcome['ok'], outcome
    assert outcome['data']['exit_code'] == 7
    assert (agent.state_dir / 'snapshots' / req['id'] / 'failed.json').exists()
    assert not agent._execution_snapshots


@pytest.mark.asyncio
async def test_unsupported_native_snapshot_has_no_filesystem_effects(shell_agent):
    agent, root = shell_agent
    req = request(root, 'touch forbidden', isolation='snapshot')
    await agent.handle(req)
    outcome = result(agent, req)
    assert not outcome['ok']
    assert outcome['error']['code'] == 'SNAPSHOT_RUNTIME_UNSUPPORTED'
    assert not (root / '.codepier').exists()
    assert not (agent.state_dir / 'snapshots').exists()


@pytest.mark.asyncio
async def test_disabled_shell_never_creates_run_or_snapshot(shell_agent):
    agent, root = shell_agent
    clean(agent)
    agent.config['shell']['enabled'] = False
    req = request(root, 'touch forbidden', isolation='snapshot')
    await agent.handle(req)
    assert result(agent, req)['error']['code'] == 'SHELL_DISABLED'
    assert not (root / '.codepier').exists()
    assert not (agent.state_dir / 'snapshots').exists()


@pytest.mark.asyncio
async def test_fail_fast_exec_has_no_run_directory(shell_agent):
    agent, root = shell_agent
    req = request(root, 'touch forbidden', resources=[{'name': '.', 'mode': 'write'}],
                  options={'wait_policy': 'fail_fast'}, yield_seconds=0)
    async with agent.scheduler.slot('holder', 'other-project', claims=[
            Claim('agent', 'path', canonical_path(root), True, 'process')]):
        await agent.handle(req)
    assert result(agent, req)['error']['code'] == 'RESOURCE_BUSY'
    assert not (root / '.codepier').exists()
    assert not (root / 'forbidden').exists()


@pytest.mark.asyncio
async def test_snapshot_rejects_fail_fast_before_copy(shell_agent):
    agent, root = shell_agent
    clean(agent)
    req = request(root, isolation='snapshot', options={'wait_policy': 'fail_fast'})
    await agent.handle(req)
    assert result(agent, req)['error']['code'] == 'INVALID_ARGUMENT'
    assert not (agent.state_dir / 'snapshots').exists()


@pytest.mark.asyncio
async def test_cancelled_clone_waits_for_worker_and_marks_failure(shell_agent, monkeypatch):
    agent, root = shell_agent
    clean(agent)
    (root / 'source').write_text('x')
    original = SnapshotManager.create
    entered, release = threading.Event(), threading.Event()
    def delayed(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)
    monkeypatch.setattr('agent.snapshots._clone_file', shutil.copy2)
    monkeypatch.setattr(SnapshotManager, 'create', delayed)
    req = request(root, isolation='snapshot')
    from shared.core_contracts import Exec
    args = Exec.model_validate(req['args']).model_dump()
    work = asyncio.create_task(agent.prepare_snapshot(req['id'], req['project'], args, root.resolve(), time.time() + 60))
    async with asyncio.timeout(5):
        while not entered.is_set():
            if work.done():
                await work
            await asyncio.sleep(.01)
    work.cancel()
    await asyncio.sleep(.01)
    assert not work.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert (agent.state_dir / 'snapshots' / req['id'] / 'failed.json').exists()
    assert not agent._execution_snapshots
    assert not agent.scheduler.active


@pytest.mark.asyncio
async def test_runtime_change_after_clone_does_not_launch(shell_agent, monkeypatch):
    agent, root = shell_agent
    clean(agent)
    (root / 'source').write_text('x')
    monkeypatch.setattr('agent.snapshots._clone_file', shutil.copy2)
    original = agent.prepare_snapshot
    async def changed(*args, **kwargs):
        await original(*args, **kwargs)
        agent.config['project_runtimes'] = validate_runtimes({'fixture': {'mode': 'project_clean_v1', 'env': {'CHANGED': 'yes'}}})
    monkeypatch.setattr(agent, 'prepare_snapshot', changed)
    req = request(root, 'touch forbidden', isolation='snapshot')
    await agent.handle(req)
    assert result(agent, req)['error']['code'] == 'RUNTIME_CONFIGURATION_CHANGED'
    assert not (root / 'forbidden').exists()
    assert not agent.processes
    assert (agent.state_dir / 'snapshots' / req['id'] / 'failed.json').exists()


@pytest.mark.asyncio
async def test_snapshot_process_cancellation_stops_child_before_retention(shell_agent, monkeypatch):
    agent, root = shell_agent
    clean(agent)
    monkeypatch.setattr('agent.snapshots._clone_file', shutil.copy2)
    (root / 'source').write_text('original')
    req = request(root, 'printf started > "$CODEPIER_RUN_DIR/started"; sleep 20; touch forbidden',
                  isolation='snapshot')
    work = asyncio.create_task(agent.handle(req))
    async with asyncio.timeout(5):
        while not (root / '.codepier/runs' / req['id'] / 'started').exists():
            if work.done():
                await work
                pytest.fail(str(result(agent, req)))
            await asyncio.sleep(.01)
    agent.cancelled.add(req['id'])
    work.cancel()
    await asyncio.gather(work, return_exceptions=True)
    assert not agent.processes
    container = agent.state_dir / 'snapshots' / req['id']
    assert (container / 'failed.json').exists()
    assert not (container / 'tree/forbidden').exists()
    marker = root / '.codepier/runs' / req['id'] / '.codepier-run.json'
    assert json.loads(marker.read_text())['finished']
    assert not agent._execution_snapshots


@pytest.mark.asyncio
async def test_shell_permission_change_after_clone_prevents_launch(shell_agent, monkeypatch):
    agent, root = shell_agent
    clean(agent)
    (root / 'source').write_text('x')
    monkeypatch.setattr('agent.snapshots._clone_file', shutil.copy2)
    original = agent.prepare_snapshot
    async def revoked(*args, **kwargs):
        await original(*args, **kwargs)
        agent.config['shell']['enabled'] = False
    monkeypatch.setattr(agent, 'prepare_snapshot', revoked)
    req = request(root, 'touch forbidden', isolation='snapshot')
    await agent.handle(req)
    assert result(agent, req)['error']['code'] == 'SHELL_DISABLED'
    assert not (root / '.codepier').exists()
    assert (agent.state_dir / 'snapshots' / req['id'] / 'failed.json').exists()


@pytest.mark.asyncio
async def test_snapshot_releases_source_claim_during_process(shell_agent, monkeypatch):
    agent, root = shell_agent
    clean(agent)
    monkeypatch.setattr('agent.snapshots._clone_file', shutil.copy2)
    (root / 'source').write_text('original')
    req = request(root, 'printf started > "$CODEPIER_RUN_DIR/started"; sleep .3; cat source', isolation='snapshot')
    work = asyncio.create_task(agent.handle(req))
    async with asyncio.timeout(5):
        while not (root / '.codepier/runs' / req['id'] / 'started').exists():
            if work.done():
                await work
                pytest.fail(str(result(agent, req)))
            await asyncio.sleep(.01)
        async with agent.scheduler.slot('edit', 'other-project', lane='read', claims=[
                Claim('agent', 'path', canonical_path(root / 'source'), True, 'file')]):
            (root / 'source').write_text('new')
    await work
    assert result(agent, req)['data']['output'] == 'original'
    assert (root / 'source').read_text() == 'new'
