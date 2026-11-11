"""P13: real local repositories, no network credentials or user checkouts."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
import uuid

import pytest

from agent.git_freshness import freshness
from agent.runner import Agent as RuntimeAgent
from agent.workspaces import Workspaces
from shared.integration_contracts import WorktreeCreate
from shared.util import DevError


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    remote = tmp_path / 'remote.git'
    git(tmp_path, 'init', '--bare', '--initial-branch=main', str(remote))
    seed = tmp_path / 'seed'
    git(tmp_path, 'clone', str(remote), str(seed))
    git(seed, 'config', 'user.name', 'Synthetic Test')
    git(seed, 'config', 'user.email', 'test@example.invalid')
    (seed / 'source.txt').write_text('one')
    git(seed, 'add', '.')
    git(seed, 'commit', '-m', 'one')
    git(seed, 'push', 'origin', 'main')
    checkout = tmp_path / 'checkout'
    git(tmp_path, 'clone', str(remote), str(checkout))
    first = git(checkout, 'rev-parse', 'HEAD')
    (seed / 'source.txt').write_text('two')
    git(seed, 'commit', '-am', 'two')
    git(seed, 'push', 'origin', 'main')
    return checkout, remote, first, git(seed, 'rev-parse', 'HEAD')


class Records:
    def __init__(self):
        self.rows = {}

    def save(self, kind, identifier, project, row, **kwargs):
        self.rows[identifier] = dict(row)

    def list_page(self, kind, project, limit=100):
        return {'records': list(self.rows.values())[:limit], 'record_errors': []}

    def load(self, kind, identifier, project):
        return dict(self.rows[identifier])


class Engine:
    def root(self, project, write=False):
        return Path(project['root']), {'allow_tasks': True}

    def check_local_write(self, path):
        pass


class Agent:
    disk_call = staticmethod(RuntimeAgent.disk_call)

    def __init__(self, tmp_path):
        self.state_dir = tmp_path / 'state'
        self.config = {}
        self.engine = Engine()
        self.native = SimpleNamespace(live=lambda: [])
        self._active_slots = {}

    async def run_process(self, identifier, command, cwd, timeout, **kwargs):
        proc = await asyncio.create_subprocess_exec(*command, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        output, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return {'exit_code': proc.returncode, 'output': output.decode(),
                'timed_out': False, 'cancelled': False}

    @asynccontextmanager
    async def project_slot(self, root, write=True):
        yield


def service(tmp_path, root):
    return Workspaces(Agent(tmp_path), Records()), {
        'id': 'synthetic', 'alias': 'synthetic', 'root': str(root),
        'allow_tasks': True, 'mode': 'write', '_integration_owner': 'synthetic'}


@pytest.mark.asyncio
async def test_auto_base_fetches_remote_and_preserves_dirty_source(tmp_path, repos):
    root, remote, first, latest = repos
    manager, project = service(tmp_path, root)
    (root / 'uncommitted.txt').write_text('keep')
    args = WorktreeCreate(project='synthetic', idempotency_key=uuid.uuid4().hex).model_dump()
    result = await manager.create(uuid.uuid4().hex, project, args)
    assert result['base_commit'] == latest
    assert result['base']['source_head_behind'] == 1
    assert result['base']['fetched'] is True
    assert git(root, 'rev-parse', 'HEAD') == first
    assert (root / 'uncommitted.txt').read_text() == 'keep'
    assert not (Path(result['path']) / 'uncommitted.txt').exists()
    status = freshness(root)
    assert status['behind'] == 1 and status['dirty'] == 1
    assert status['network_checked'] is False and status['last_fetch_age_seconds'] is not None


@pytest.mark.asyncio
async def test_explicit_base_and_named_branch_remain_exact(tmp_path, repos):
    root, _, first, _ = repos
    manager, project = service(tmp_path, root)
    args = WorktreeCreate(project='synthetic', idempotency_key=uuid.uuid4().hex,
                          base_ref=first, fetch=False, branch='fix/audit').model_dump()
    result = await manager.create(uuid.uuid4().hex, project, args)
    assert result['base_commit'] == first
    assert result['base']['fetched'] is False
    assert git(Path(result['path']), 'branch', '--show-current') == 'fix/audit'


@pytest.mark.asyncio
async def test_fetch_failure_is_visible_and_falls_back_to_cached_refs(tmp_path, repos):
    root, _, first, _ = repos
    git(root, 'remote', 'set-url', 'origin', str(tmp_path / 'missing.git'))
    manager, project = service(tmp_path, root)
    commit, report = await manager.resolve_base('read', project, root)
    assert commit == first and report['fetch_error'] == 'WORKTREE_GIT_FAILED'
    assert report['freshness'] == 'local_refs_only'


@pytest.mark.asyncio
async def test_no_remote_has_explicit_local_fallback(tmp_path):
    git(tmp_path, 'init', '--initial-branch=main')
    git(tmp_path, 'config', 'user.name', 'Synthetic Test')
    git(tmp_path, 'config', 'user.email', 'test@example.invalid')
    git(tmp_path, 'commit', '--allow-empty', '-m', 'empty')
    manager, project = service(tmp_path, tmp_path)
    commit, report = await manager.resolve_base('read', project, tmp_path)
    assert commit == git(tmp_path, 'rev-parse', 'HEAD')
    assert report['resolved'] == 'HEAD' and report['fetch_error']


@pytest.mark.asyncio
async def test_list_only_proposes_clean_upstream_merged_worktrees(tmp_path, repos):
    root, _, _, _ = repos
    manager, project = service(tmp_path, root)
    args = WorktreeCreate(project='synthetic', idempotency_key=uuid.uuid4().hex).model_dump()
    result = await manager.create(uuid.uuid4().hex, project, args)
    rows = (await manager.list('list', project))['workspaces']
    assert rows[0]['cleanup_candidate'] is True
    assert rows[0]['cleanup']['arguments']['confirm'] == result['workspace_id']
    (Path(result['path']) / 'keep.txt').write_text('uncommitted')
    rows = (await manager.list('list', project))['workspaces']
    assert rows[0]['dirty'] and not rows[0]['cleanup_candidate']
    assert Path(result['path']).exists()


@pytest.mark.asyncio
async def test_cancelled_fetch_never_falls_back(tmp_path, repos, monkeypatch):
    root, _, _, _ = repos
    manager, project = service(tmp_path, root)
    async def cancelled(*args, **kwargs):
        raise DevError('WORKTREE_CANCELLED', 'cancelled')
    monkeypatch.setattr(manager, 'git', cancelled)
    with pytest.raises(DevError) as error:
        await manager.resolve_base('read', project, root)
    assert error.value.code == 'WORKTREE_CANCELLED'


@pytest.mark.asyncio
async def test_invalid_explicit_base_does_not_fall_back(tmp_path, repos):
    root, _, _, _ = repos
    manager, project = service(tmp_path, root)
    with pytest.raises(DevError) as error:
        await manager.resolve_base('read', project, root, 'missing-ref', False)
    assert error.value.code == 'GIT_BASE_REQUIRED'


def test_freshness_ignores_inherited_git_routing(repos, monkeypatch, tmp_path):
    root, _, first, _ = repos
    monkeypatch.setenv('GIT_DIR', str(tmp_path / 'wrong.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(tmp_path))
    result = freshness(root)
    assert result['available'] and result['head'] == first


def test_public_open_keeps_freshness_but_drops_unknown_git_fields():
    from shared.mcp_presentation import _open
    result = _open({'git': {'available': True, 'behind': 3, 'network_checked': False,
                           'secret': 'must-not-leak'}, 'context': None}, {})
    assert result['git'] == {'available': True, 'behind': 3, 'network_checked': False}


def test_non_repository_freshness_is_explicit(tmp_path):
    assert freshness(tmp_path) == {
        'available': False, 'reason': 'not_a_git_root', 'network_checked': False}


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['create', 'list', 'remove'])
async def test_worktree_storage_operations_do_not_block_the_event_loop(tmp_path, repos, monkeypatch, action):
    root, _, _, _ = repos
    manager, project = service(tmp_path, root)
    identifier = uuid.uuid4().hex
    args = WorktreeCreate(project='synthetic', idempotency_key=identifier, fetch=False).model_dump()
    if action != 'create':
        await manager.create(identifier, project, args)
    loop_thread = threading.get_ident()
    observed = []

    def off_loop(name, function):
        def call(*values, **keywords):
            observed.append(name)
            assert threading.get_ident() != loop_thread, f'{name} blocked the event loop'
            return function(*values, **keywords)
        return call

    for obj, names in [(manager.agent.engine, ['root', 'check_local_write']),
                       (manager.records, ['load', 'list_page', 'save']),
                       (manager.agent.native, ['live'])]:
        for name in names:
            monkeypatch.setattr(obj, name, off_loop(name, getattr(obj, name)))
    if action == 'create':
        assert (await manager.create(identifier, project, args))['state'] == 'ready'
    elif action == 'list':
        assert (await manager.list('list', project))['workspaces'][0]['state'] == 'ready'
    else:
        assert (await manager.remove('remove', project, {
            'target_workspace_id': identifier, 'confirm': identifier}))['removed'] is True
    assert 'root' in observed and ('save' in observed if action != 'list' else 'load' in observed)


@pytest.mark.asyncio
async def test_worktree_list_resolves_one_shared_upstream_per_listing(tmp_path, repos, monkeypatch):
    root, _, _, _ = repos
    manager, project = service(tmp_path, root)
    for _ in range(2):
        identifier = uuid.uuid4().hex
        args = WorktreeCreate(project='synthetic', idempotency_key=identifier, fetch=False).model_dump()
        await manager.create(identifier, project, args)
    original = manager.resolve_base
    calls = []

    async def resolve(*values, **keywords):
        calls.append((values, keywords))
        return await original(*values, **keywords)

    monkeypatch.setattr(manager, 'resolve_base', resolve)
    rows = (await manager.list('list', project))['workspaces']
    assert len(rows) == 2 and all(row['cleanup_candidate'] for row in rows)
    assert len(calls) == 1 and calls[0][1] == {'fetch': False}


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['initial_save', 'ready_save', 'removed_save'])
async def test_worktree_cancellation_waits_for_receipt_storage(tmp_path, repos, monkeypatch, stage):
    root, _, _, _ = repos
    manager, project = service(tmp_path, root)
    identifier = uuid.uuid4().hex
    args = WorktreeCreate(project='synthetic', idempotency_key=identifier, fetch=False).model_dump()
    if stage == 'removed_save':
        await manager.create(identifier, project, args)
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    io_threads = []
    original = manager.records.save

    def save(kind, saved_id, bound, row, **keywords):
        block = (stage == 'initial_save' and not keywords.get('replace')
                 or stage == 'ready_save' and row['state'] == 'ready'
                 or stage == 'removed_save' and row['state'] == 'removed')
        if block:
            io_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(2):
                raise AssertionError('event loop did not release receipt storage')
        return original(kind, saved_id, bound, row, **keywords)

    monkeypatch.setattr(manager.records, 'save', save)
    operation = (manager.remove('remove', project, {'target_workspace_id': identifier, 'confirm': identifier})
                 if stage == 'removed_save' else manager.create(identifier, project, args))
    work = asyncio.create_task(operation)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), .5)
        assert len(io_threads) == 1 and io_threads[0] != loop_thread
        work.cancel()
        await asyncio.sleep(0)
        work.cancel()
        await asyncio.sleep(0)
        assert not work.done(), 'receipt storage must settle before cancellation escapes'
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await work
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)
    record = manager.records.rows[identifier]
    assert record['state'] == ('removed' if stage == 'removed_save' else 'needs_review')
    assert Path(record['path']).exists() is (stage == 'ready_save')


@pytest.mark.asyncio
async def test_worktree_final_save_failure_leaves_reviewable_receipt(tmp_path, repos, monkeypatch):
    root, _, _, _ = repos
    manager, project = service(tmp_path, root)
    identifier = uuid.uuid4().hex
    args = WorktreeCreate(project='synthetic', idempotency_key=identifier, fetch=False).model_dump()
    original = manager.records.save

    def save(kind, saved_id, bound, row, **keywords):
        if row['state'] == 'ready':
            raise OSError('synthetic final save failure')
        return original(kind, saved_id, bound, row, **keywords)

    monkeypatch.setattr(manager.records, 'save', save)
    with pytest.raises(OSError, match='synthetic final save'):
        await manager.create(identifier, project, args)
    record = manager.records.rows[identifier]
    assert record['state'] == 'needs_review' and Path(record['path']).is_dir()


@pytest.mark.asyncio
async def test_duplicate_worktree_create_preserves_original_receipt(tmp_path, repos, monkeypatch):
    root, _, _, _ = repos
    manager, project = service(tmp_path, root)
    identifier = uuid.uuid4().hex
    previous = {'state': 'ready', 'sentinel': 'keep existing evidence'}
    manager.records.rows[identifier] = previous.copy()
    original = manager.records.save

    def save(kind, saved_id, bound, row, **keywords):
        if not keywords.get('replace'):
            raise DevError('RECORD_EXISTS', 'existing receipt', 409)
        return original(kind, saved_id, bound, row, **keywords)

    monkeypatch.setattr(manager.records, 'save', save)
    args = WorktreeCreate(project='synthetic', idempotency_key=identifier, fetch=False).model_dump()
    with pytest.raises(DevError) as caught:
        await manager.create(identifier, project, args)
    assert caught.value.code == 'RECORD_EXISTS'
    assert manager.records.rows[identifier] == previous
