"""P13: real local repositories, no network credentials or user checkouts."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import subprocess
from types import SimpleNamespace
import uuid

import pytest

from agent.git_freshness import freshness
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

    def list(self, kind, project, limit=100):
        return list(self.rows.values())[:limit]

    def load(self, kind, identifier, project):
        return dict(self.rows[identifier])


class Engine:
    def root(self, project, write=False):
        return Path(project['root']), {'allow_tasks': True}

    def check_local_write(self, path):
        pass


class Agent:
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
