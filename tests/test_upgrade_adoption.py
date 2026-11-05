"""Canonical installations get the same recoverable cutover as migrations."""
import copy
import json
import sys
from types import SimpleNamespace
import pytest
from scripts import migrate_hub as upgrade
from tests.test_codepier_hub_migration import Docker


class CanonicalDocker(Docker):
    def __init__(self, journal):
        super().__init__()
        self.current = copy.deepcopy(self.old)
        self.current[0]['Mounts'][0]['Name'] = 'codepier-hub-data'
        self.old = []
        self.volumes = {'codepier-hub-data': {'Labels': {}}}
        self.journal = journal
        self.foreign = False

    def containers(self, project):
        rows = copy.deepcopy(self.current) if project == 'codepier' else []
        for row in rows:
            row['State'] = {'Running': not self.stopped, 'Health': {'Status': 'healthy'}}
        return rows

    def run(self, args, **kwargs):
        if args[0] in {'update', 'stop'}:
            saved = json.loads(self.journal.read_text())
            assert saved['containers'][0]['stop_requested']
        result = super().run(args, **kwargs)
        if args[:2] == ['ps', '-q'] and args[-1] == 'volume=codepier-hub-data' and not self.stopped:
            return SimpleNamespace(returncode=0, stdout='b' * 12 if self.foreign else 'a' * 12)
        return result


@pytest.mark.parametrize('prior_completed', [False, True])
def test_current_installation_requires_new_boundary_and_health(tmp_path, monkeypatch, prior_completed):
    path = tmp_path / upgrade.STATE
    if prior_completed:
        path.write_text(json.dumps({'stage': 'completed', 'transaction': 'old'}))
    docker = CanonicalDocker(path)
    state = upgrade.prepare(path, docker)
    assert state['stage'] == 'prepared' and state['adopted_existing']
    assert state['transaction'] != 'old' and not state['write_boundary']
    assert docker.stopped and state['networks'] == []
    assert not state['volumes'][0].get('create_requested')
    monkeypatch.setattr(upgrade, 'Docker', lambda: docker)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['upgrade', 'write-boundary', '--root', str(tmp_path)])
    assert upgrade.main() == 0
    assert json.loads(path.read_text())['stage'] == 'starting'
    monkeypatch.setattr(sys, 'argv', ['upgrade', 'commit', '--root', str(tmp_path)])
    assert upgrade.main() == 1
    docker.stopped = False
    assert upgrade.main() == 0
    assert json.loads(path.read_text())['stage'] == 'completed'
    assert ['update', '--restart=unless-stopped', 'a' * 64] in docker.commands


def test_canonical_probe_failure_restores_old_container_and_keeps_volume(tmp_path):
    path = tmp_path / upgrade.STATE
    docker = CanonicalDocker(path)
    upgrade.prepare(path, docker)
    state = upgrade.rollback(path, docker)
    assert state['stage'] == 'rolled_back' and not docker.stopped
    assert set(docker.volumes) == {'codepier-hub-data'}
    assert not any(command[:2] == ['volume', 'rm'] for command in docker.commands)
    assert ['update', '--restart=unless-stopped', 'a' * 64] in docker.commands


def test_foreign_canonical_writer_is_rejected_before_stopping(tmp_path):
    path = tmp_path / upgrade.STATE
    docker = CanonicalDocker(path)
    docker.foreign = True
    with pytest.raises(RuntimeError, match='another container'):
        upgrade.prepare(path, docker)
    assert not docker.stopped and not path.exists()


def test_canonical_restart_restore_failure_cannot_commit(tmp_path, monkeypatch):
    path = tmp_path / upgrade.STATE
    docker = CanonicalDocker(path)
    state = upgrade.prepare(path, docker)
    upgrade.save(path, state, stage='starting', write_boundary=True)
    docker.stopped = False
    original = docker.run
    def fail_restore(args, **kwargs):
        if args[:2] == ['update', '--restart=unless-stopped']:
            raise RuntimeError('Synthetic restart policy update failure')
        return original(args, **kwargs)
    docker.run = fail_restore
    monkeypatch.setattr(upgrade, 'Docker', lambda: docker)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['upgrade', 'commit', '--root', str(tmp_path)])
    assert upgrade.main() == 1
    assert json.loads(path.read_text())['stage'] == 'starting'
