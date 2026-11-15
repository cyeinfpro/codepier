"""Committed recovery never replays cutover or rolls back accepted writes."""
from types import SimpleNamespace

from scripts import panel_updater as updater
from scripts.panel_update_runtime import atomic_json, read_json
from tests.test_panel_update import manager_fixture, body_for, TARGET


def committed_job(tmp_path):
    manager, release = manager_fixture(tmp_path)
    manager.submit('apply', body_for(release))
    job = manager.current_job()
    manager.acquire_install_lock(job)
    manager.runtime.commit(job)
    manager.save(job, state='recovery_required', phase='committed', commit_decided=True,
                 error={'code': 'ENGINE_PROMOTION', 'message': 'prior finalization failure'})
    manager.runtime.calls.clear()
    return manager, job


def test_manual_committed_recovery_only_verifies_and_clears_gate(tmp_path, monkeypatch):
    manager, job = committed_job(tmp_path)
    monkeypatch.setattr(manager, 'perform', lambda *_: (_ for _ in ()).throw(AssertionError('replay')))
    manager.recover()  # Daemon startup must not silently resolve explicit recovery_required.
    assert manager.runtime.calls == []
    assert manager.current_job()['state'] == 'recovery_required'
    manager.recover(manual=True)
    assert manager.runtime.calls == ['set_gate', 'verify', 'clear_gate']
    assert manager.current_job()['state'] == 'succeeded'
    assert manager.current_job()['error']['code'] == 'ENGINE_PROMOTION'
    assert read_json(manager.home / 'current.json')['version'] == TARGET
    assert not (tmp_path / '.codepier-install.lock').exists()
    assert not manager.runtime.gate.exists()
    assert manager.submit('apply', body_for(read_json(manager.home / 'candidate.json')))['replayed']
    manager.recover(manual=True)
    assert manager.runtime.calls == ['set_gate', 'verify', 'clear_gate']


def test_failed_committed_verification_never_rolls_back(tmp_path):
    manager, job = committed_job(tmp_path)
    manager.runtime.fail = 'verify'
    manager.recover(manual=True)
    assert manager.runtime.calls == ['set_gate', 'verify']
    assert manager.current_job()['state'] == 'recovery_required'
    assert manager.current_job()['commit_decided'] is True
    assert read_json(manager.home / 'current.json')['version'] == TARGET
    assert manager.runtime.gate.exists()
    assert (tmp_path / '.codepier-install.lock').exists()


def test_restart_promotes_only_after_manual_recovery(tmp_path):
    manager, job = committed_job(tmp_path)
    old, new = {'id': 'old'}, {'id': 'new'}
    atomic_json(manager.home / 'engine-current.json', old)
    manager.save(job, engine=old, staged_engine=new)
    manager.recover(manual=True)
    restarted = updater.Manager(tmp_path, runtime=manager.runtime, start_workers=False)
    promotions = []
    def promote(home, identity, job_id):
        saved = read_json(home / 'jobs' / job_id / 'status.json')
        assert saved['state'] == 'succeeded' and saved['commit_decided']
        assert restarted.draining is True
        assert manager.runtime.calls == ['set_gate', 'verify', 'clear_gate']
        promotions.append((identity, job_id))
        atomic_json(home / 'engine-current.json', identity)
    restarted.engine_identity = old
    restarted.engine_helper = SimpleNamespace(promote_engine=promote)
    restarted.activate_staged_engine(restarted.current_job())
    assert promotions == [(new, job['id'])]
    assert restarted.restart_requested is True
    restarted.activate_staged_engine(restarted.current_job())
    assert len(promotions) == 1
