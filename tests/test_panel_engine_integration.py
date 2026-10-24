"""Manager integration contracts; no Docker or host installation is invoked."""
import copy
from pathlib import Path
import time

import pytest

from scripts import panel_updater as updater
from scripts.panel_update_runtime import atomic_json, read_json
from scripts.panel_update_source import UpdateError
from shared.util import VERSION

TARGET = '.'.join([VERSION.split('.')[0], str(int(VERSION.split('.')[1]) + 1), '0'])
OLD = {'id': 'a' * 64, 'version': VERSION}
NEW = {'id': 'b' * 64, 'version': TARGET}


class Runtime:
    def __init__(self, home):
        self.calls = []
        self.home = home
        self.gate = home / 'run/maintenance.json'

    def prepare(self, job, source):
        self.calls.append('prepare')
        return {'old_volume': 'old', 'new_volume': 'new'}

    def build(self, job, source):
        self.calls.append('build')
        return {'version': TARGET}

    def quiesce(self, job):
        self.calls.append('quiesce')

    def stop(self, job):
        self.calls.append('stop')

    def copy_data(self, job):
        self.calls.append('copy')
        return {'integrity': 'ok'}

    def start(self, job, which):
        self.calls.append('start_' + which)

    def verify(self, job):
        self.calls.append('verify')

    def commit(self, job):
        self.calls.append('commit')
        atomic_json(self.home / 'current.json', {'version': TARGET})

    def clear_gate(self):
        self.calls.append('clear_gate')

    def set_gate(self, job):
        self.calls.append('set_gate')

    def rollback(self, job):
        self.calls.append('rollback')


class Helper:
    def __init__(self, runtime):
        self.runtime = runtime
        self.wrong_owner = False
        self.stage_error = False
        self.promotion_error = False

    def verify_engine(self, home, identity):
        return home / 'other-engine' if self.wrong_owner else Path(updater.__file__).resolve().parent

    def stage_engine(self, home, source, release):
        self.runtime.calls.append('stage')
        if self.stage_error:
            raise RuntimeError('unsupported engine')
        return copy.deepcopy(NEW)

    def promote_engine(self, home, identity, job_id):
        self.runtime.calls.append('promote')
        assert read_json(home / 'jobs' / job_id / 'status.json')['state'] == 'succeeded'
        if self.promotion_error:
            raise RuntimeError('promotion interrupted')
        atomic_json(home / 'engine-current.json', identity)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / '.codepier-updater'; home.mkdir()
    atomic_json(home / 'config.json', {'repository': 'cyeinfpro/codepier', 'project': 'fixture'})
    atomic_json(home / 'current.json', {'version': VERSION})
    release = {'repository': 'cyeinfpro/codepier', 'version': TARGET, 'release_id': 1, 'asset_id': 2,
               'sha256': 'c' * 64, 'bytes': 1, 'url': 'https://example.invalid/source.zip', 'checked_at': time.time()}
    atomic_json(home / 'candidate.json', release)
    atomic_json(home / 'engine-current.json', OLD)
    runtime = Runtime(home)
    manager = updater.Manager(tmp_path, runtime=runtime, start_workers=False)
    helper = Helper(runtime)
    manager.engine_helper = helper
    manager.engine_identity = copy.deepcopy(OLD)
    monkeypatch.setattr(updater, 'latest_release', lambda repository: release)
    monkeypatch.setattr(updater, 'fetch', lambda *args, **kwargs: kwargs['destination'].write_bytes(b'x'))
    monkeypatch.setattr(updater, 'unpack_bundle', lambda archive, destination, release: destination)
    body = {'idempotency_key': 'engine-integration-123', 'current_version': VERSION, 'actor': 'panel-admin',
            **{key: release[key] for key in ('version', 'release_id', 'sha256')}}
    manager.submit('apply', body)
    return manager, helper, runtime, body


def test_stage_before_stop_and_idle_promotion_requests_restart(setup):
    manager, helper, runtime, body = setup
    manager.perform(manager.current_job())
    assert manager.current_job()['state'] == 'succeeded'
    assert runtime.calls.index('stage') < runtime.calls.index('stop') < runtime.calls.index('promote')
    assert manager.draining and manager.restart_requested
    with pytest.raises(UpdateError):
        manager.submit('check', {'idempotency_key': 'after-promotion-123', 'current_version': TARGET, 'actor': 'panel-admin'})
    assert manager.status(body['idempotency_key'])['operation']['state'] == 'succeeded'


def test_stage_failure_does_not_stop_live_hub(setup):
    manager, helper, runtime, body = setup
    helper.stage_error = True
    manager.perform(manager.current_job())
    assert manager.current_job()['state'] == 'failed'
    assert runtime.calls == ['stage']


def test_promotion_failure_after_commit_never_rolls_back(setup):
    manager, helper, runtime, body = setup
    helper.promotion_error = True
    manager.perform(manager.current_job())
    job = manager.current_job()
    assert job['commit_decided']
    assert job['state'] == 'recovery_required'
    assert 'rollback' not in runtime.calls
    assert read_json(manager.home / 'current.json')['version'] == TARGET


def test_terminal_before_promotion_can_continue_without_redeploy(setup):
    manager, helper, runtime, body = setup
    job = manager.current_job()
    manager.acquire_install_lock(job)
    manager.save(job, staged_engine=NEW, old_volume='old')
    manager.cutover(job, {'version': TARGET})
    assert not manager.restart_requested
    calls = list(runtime.calls)
    manager.activate_staged_engine(manager.current_job())
    assert runtime.calls == calls + ['promote']
    assert manager.restart_requested


def test_standalone_wrong_owner_cannot_perform(setup):
    manager, helper, runtime, body = setup
    helper.wrong_owner = True
    with pytest.raises(UpdateError, match='固定'):
        manager.perform(manager.current_job())
    assert not runtime.calls


def test_standalone_wrong_owner_cannot_recover(setup):
    manager, helper, runtime, body = setup
    job = manager.current_job()
    manager.save(job, state='running', phase='copying', old_volume='old')
    helper.wrong_owner = True
    with pytest.raises(UpdateError, match='固定'):
        manager.recover()
    assert not runtime.calls


def test_wrong_job_engine_cannot_recover(setup):
    manager, helper, runtime, body = setup
    job = manager.current_job()
    manager.save(job, state='running', phase='copying', old_volume='old', engine=NEW)
    with pytest.raises(UpdateError):
        manager.recover()
    assert not runtime.calls
