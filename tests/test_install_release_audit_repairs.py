"""Offline install/release regressions; no real services, Docker or network calls."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from scripts import install_agent as installer
from scripts import migrate_hub as migration
from scripts import panel_update_source as releases
from tests.test_codepier_hub_migration import Docker as VolumeDocker

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def systemd_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    base = home / 'new-install'
    (base / 'runtime').mkdir(parents=True)
    target = home / '.config/systemd/user/codepier-agent.service'
    target.parent.mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setattr(installer.sys, 'platform', 'linux')
    monkeypatch.setattr(installer.os, 'geteuid', lambda: 1000)
    calls = []
    monkeypatch.setattr(installer, 'run', lambda args, **kwargs: calls.append(list(args)))
    monkeypatch.setattr(installer.time, 'sleep', lambda _: None)
    return base, target, calls


@pytest.mark.parametrize('recorded', [False, True])
def test_linux_start_never_overwrites_another_installation(systemd_home, recorded):
    base, target, calls = systemd_home
    original = 'WorkingDirectory=/other/runtime\nExecStart=/other/python -m agent\n'
    target.write_text(original)
    if recorded:
        (base / 'management.json').write_text(json.dumps({'service_name': target.name}))
    with pytest.raises(ValueError, match='different'):
        installer.start_service(base, base / 'runtime/.venv/bin/python')
    assert target.read_text() == original and calls == []


def test_linux_start_refuses_symlinked_unit(systemd_home):
    base, target, calls = systemd_home
    other = base / 'do-not-change'
    other.write_text('retained')
    target.symlink_to(other)
    with pytest.raises(ValueError, match='symlink|safe'):
        installer.start_service(base, base / 'runtime/.venv/bin/python')
    assert other.read_text() == 'retained' and target.is_symlink() and calls == []


def test_linux_start_repairs_matching_unit(systemd_home):
    base, target, calls = systemd_home
    python = base / 'runtime/.venv/bin/python'
    command = [str(python), '-m', 'agent', '--config', str(base / 'config.json'), 'run']
    target.write_text('WorkingDirectory=' + str(base / 'runtime') + '\nExecStart=' +
                      ' '.join(installer.systemd_quote(value) for value in command) + '\n')
    installer.start_service(base, python)
    assert 'WorkingDirectory=' + str(base / 'runtime') in target.read_text()
    assert calls == [['systemctl', '--user', 'daemon-reload'],
                     ['systemctl', '--user', 'enable', '--now', target.name],
                     ['systemctl', '--user', 'is-active', '--quiet', target.name]]


def test_official_default_install_and_release_urls(monkeypatch, tmp_path):
    from scripts import panel_updater
    repository = 'cyeinfpro/codepier'
    assert releases.DEFAULT_REPOSITORY == repository
    assert repository in (ROOT / 'docs/PANEL_UPDATE.md').read_text()
    assert 'https://github.com/' + repository + '/releases/latest' in (ROOT / 'README.md').read_text()
    called = []
    monkeypatch.setattr(panel_updater.sys, 'argv', ['panel_updater', 'install', '--root', str(tmp_path)])
    monkeypatch.setattr(panel_updater, 'install', lambda root, repo: called.append((root, repo)))
    assert panel_updater.main() == 0 and called == [(tmp_path, repository)]
    url = 'https://github.com/' + repository + '/releases/download/v1.23.1/codepier-1.23.1-source.zip'
    metadata = {'id': 1, 'draft': False, 'prerelease': False, 'tag_name': 'v1.23.1',
                'assets': [{'id': 2, 'name': 'codepier-1.23.1-source.zip', 'state': 'uploaded',
                            'digest': 'sha256:' + 'a' * 64, 'size': 100, 'browser_download_url': url}]}
    fetched = []
    def fetch(address, **kwargs):
        fetched.append(address)
        return json.dumps(metadata).encode()
    monkeypatch.setattr(releases, 'fetch', fetch)
    assert releases.latest_release()['url'] == url
    assert fetched == ['https://api.github.com/repos/' + repository + '/releases/latest']


class NetworkDocker(VolumeDocker):
    def __init__(self):
        super().__init__()
        self.config['networks'] = {'proxy': {'name': 'codepier_proxy',
            'ipam': {'config': [{'subnet': '172.30.87.0/24'}]}}}
        self.config['services']['hub']['networks'] = {'proxy': {}}
        self.old[0]['NetworkSettings'] = {'Networks': {'remote-dev-mcp_proxy': {
            'IPAddress': '172.30.87.3', 'Aliases': ['hub'], 'IPAMConfig': None}}}
        self.original = {'Id': 'b' * 64, 'Name': 'remote-dev-mcp_proxy', 'Driver': 'bridge',
            'Scope': 'local', 'Labels': {'com.docker.compose.project': 'remote-dev-mcp',
                'com.docker.compose.network': 'proxy'}, 'IPAM': {'Driver': 'default',
                'Config': [{'Subnet': '172.30.87.0/24', 'Gateway': '172.30.87.1'}]},
            'Options': {}, 'Containers': {'a' * 64: {}}, 'Internal': False,
            'EnableIPv6': False, 'Attachable': False}
        self.networks = {self.original['Name']: copy.deepcopy(self.original)}
        self.stopped_attachment = False

    def find(self, name):
        return next(value for value in self.networks.values() if name in (value['Name'], value['Id']))

    def json(self, args):
        if args[:2] == ['network', 'inspect']:
            return [copy.deepcopy(self.find(name)) for name in args[2:]]
        if args[0] == 'inspect':
            return copy.deepcopy(self.old)
        return super().json(args)

    def run(self, args, **kwargs):
        if args[0] == 'network':
            self.commands.append(args)
            if args[1] == 'ls':
                field = 'Name' if '{{.Name}}' in args else 'Id'
                return SimpleNamespace(stdout='\n'.join(n[field] for n in self.networks.values()))
            if args[1] == 'disconnect':
                self.old[0]['NetworkSettings']['Networks'].clear()
                self.find(args[3])['Containers'].clear()
            elif args[1] == 'rm':
                value = self.find(args[2])
                assert not value['Containers']
                del self.networks[value['Name']]
            elif args[1] == 'create':
                if any(n['IPAM']['Config'][0]['Subnet'] == '172.30.87.0/24' for n in self.networks.values()):
                    raise RuntimeError('Pool overlaps with other one on this address space')
                restored = copy.deepcopy(self.original)
                restored['Containers'] = {}
                self.networks[restored['Name']] = restored
            elif args[1] == 'connect':
                self.old[0]['NetworkSettings']['Networks']['remote-dev-mcp_proxy'] = {'IPAddress': '172.30.87.3'}
                self.networks['remote-dev-mcp_proxy']['Containers'] = {'a' * 64: {}}
            return SimpleNamespace(stdout='', returncode=0)
        if args[:2] == ['ps', '-aq'] and args[-1].startswith('network='):
            self.commands.append(args)
            value = self.find(args[-1].split('=', 1)[1])
            return SimpleNamespace(stdout='a' * 64 if value['Containers'] or self.stopped_attachment else '')
        return super().run(args, **kwargs)

    def create_probe_network(self, snapshot):
        config = snapshot['networks']['proxy']
        value = copy.deepcopy(self.original)
        value.update(Id='c' * 64, Name=config['name'], Containers={},
            Labels={**config['labels'], 'com.docker.compose.project': 'codepier',
                    'com.docker.compose.network': 'proxy'})
        self.networks[value['Name']] = value


def prepare_network_probe(tmp_path):
    docker = NetworkDocker()
    path = tmp_path / migration.STATE
    migration.prepare(path, docker)
    assert docker.stopped
    snapshot = migration.probe_compose(path, docker)
    value = json.loads(snapshot.read_text())
    assert not any(key.startswith('com.docker.compose.') for key in value['networks']['proxy']['labels'])
    docker.create_probe_network(value)
    return path, docker, snapshot


@pytest.mark.parametrize('observe_before_failure', [False, True])
def test_readonly_probe_failure_restores_original_stack(tmp_path, observe_before_failure):
    path, docker, _ = prepare_network_probe(tmp_path)
    if observe_before_failure:
        migration.probe_finished(path, docker)
    result = migration.rollback(path, docker)
    assert result['stage'] == 'rolled_back' and not docker.stopped
    assert 'codepier_proxy' not in docker.networks
    assert docker.old[0]['NetworkSettings']['Networks']['remote-dev-mcp_proxy']['IPAddress'] == '172.30.87.3'
    row = json.loads(path.read_text())['replacement_networks'][0]
    assert row['id'] == 'c' * 64 and row['remove_requested'] and row['removed']
    removed = docker.commands.index(['network', 'rm', 'c' * 64])
    restored = next(i for i, c in enumerate(docker.commands) if c[:2] == ['network', 'create'])
    restarted = next(i for i, c in enumerate(docker.commands) if c[0] == 'start')
    assert removed < restored < restarted
    assert migration.rollback(path, docker)['stage'] == 'rolled_back'


@pytest.mark.parametrize('problem', ['foreign', 'project', 'identity', 'name', 'subnet', 'options', 'endpoint', 'stopped-endpoint'])
def test_rollback_preserves_ambiguous_or_occupied_network(tmp_path, problem):
    path, docker, _ = prepare_network_probe(tmp_path)
    migration.probe_finished(path, docker)
    replacement = docker.networks['codepier_proxy']
    if problem == 'foreign':
        replacement['Labels']['com.codepier.migration'] = 'f' * 32
    elif problem == 'project':
        replacement['Labels']['com.docker.compose.project'] = 'other'
    elif problem == 'identity':
        replacement['Id'] = 'e' * 64
    elif problem == 'name':
        replacement['Name'] = 'other'
    elif problem == 'subnet':
        replacement['IPAM']['Config'][0]['Subnet'] = '10.1.0.0/24'
    elif problem == 'options':
        replacement['Options'] = {'foreign': 'value'}
    elif problem == 'endpoint':
        replacement['Containers'] = {'d' * 64: {}}
    else:
        docker.stopped_attachment = True
    before = len(docker.commands)
    with pytest.raises(RuntimeError):
        migration.rollback(path, docker)
    assert 'codepier_proxy' in docker.networks
    assert not any(c[:2] == ['network', 'rm'] for c in docker.commands[before:])
    assert json.loads(path.read_text())['stage'] == 'recovery_required'


def test_probe_refuses_preexisting_replacement(tmp_path):
    docker = NetworkDocker()
    path = tmp_path / migration.STATE
    migration.prepare(path, docker)
    docker.networks['occupied'] = {'Name': 'codepier_proxy', 'Id': 'e' * 64}
    with pytest.raises(RuntimeError, match='already exists'):
        migration.probe_compose(path, docker)
    assert not any(c == ['network', 'rm', 'e' * 64] for c in docker.commands)


def test_probe_snapshot_is_private_reusable_and_preserves_literal_dollars(tmp_path):
    docker = NetworkDocker()
    docker.config['services']['hub']['environment'] = {'LITERAL': 'value$$literal'}
    path = tmp_path / migration.STATE
    migration.prepare(path, docker)
    config = copy.deepcopy(docker.config)
    snapshot = migration.probe_compose(path, docker)
    assert snapshot.stat().st_mode & 0o077 == 0
    assert migration.probe_compose(path, docker) == snapshot and docker.config == config
    assert json.loads(snapshot.read_text())['services']['hub']['environment']['LITERAL'] == 'value$$literal'
    docker.config['services']['hub']['environment']['LITERAL'] = 'changed'
    with pytest.raises(RuntimeError, match='configuration changed'):
        migration.probe_compose(path, docker)
    docker.config = config
    snapshot.write_text('{}')
    with pytest.raises(RuntimeError, match='snapshot changed'):
        migration.probe_compose(path, docker)


def test_installer_scopes_transaction_snapshot_to_readonly_probe():
    text = (ROOT / 'deploy/install-hub.sh').read_text()
    assert 'COMPOSE_FILE="$probe_compose" docker compose run --rm --no-deps --volume' in text
    assert text.index('migrate_hub.py probe-finished') < text.index('migrate_hub.py proxy-trust')
    assert text.index('migrate_hub.py proxy-trust') < text.index('migrate_hub.py write-boundary')
    assert 'docker compose up -d --wait' in text


@pytest.mark.parametrize('failure', [None, 'mkdir', 'write', 'snapshot', 'stop'])
def test_native_evidence_failures_always_clean_owned_worker(tmp_path, monkeypatch, failure):
    from scripts import check_native_worker_real as worker
    journal = SimpleNamespace(db=Mock())
    connection = Mock()
    connection.execute.side_effect = lambda query, args: (
        [] if 'FROM commands' in query else SimpleNamespace(fetchone=lambda: (123,)))
    monkeypatch.setattr(worker, 'database', lambda _: connection)
    def screen(*args):
        if failure == 'snapshot':
            raise OSError('snapshot failed')
        return 'synthetic screen'
    monkeypatch.setattr(worker, 'output', screen)
    child = Mock()
    alive = [True]
    child.poll.side_effect = lambda: None if alive[0] else 0
    child.wait.side_effect = lambda **kwargs: 0
    child.terminate.side_effect = lambda: alive.__setitem__(0, False)
    calls = []
    def action(kind, project, args):
        calls.append(kind)
        if kind == 'stop':
            if failure == 'stop':
                raise OSError('stop failed')
            alive[0] = False
    obj = SimpleNamespace(directory=tmp_path, action=action)
    out = tmp_path / 'missing' / 'evidence'
    original_mkdir, original_write = Path.mkdir, Path.write_text
    def mkdir(path, *args, **kwargs):
        if failure == 'mkdir' and path == out:
            raise OSError('mkdir failed')
        return original_mkdir(path, *args, **kwargs)
    def write(path, *args, **kwargs):
        if failure == 'write' and path.parent == out:
            raise OSError('write failed')
        return original_write(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'mkdir', mkdir)
    monkeypatch.setattr(Path, 'write_text', write)
    if failure:
        with pytest.raises(OSError):
            worker.finish_case(obj, 's', child, journal, out, 'pi', {'id': 'p'}, 'writer')
    else:
        assert worker.finish_case(obj, 's', child, journal, out, 'pi', {'id': 'p'}, 'writer') == (
            'synthetic screen', [], 123)
        assert (out / 'worker-real-pi.ansi').read_text() == 'synthetic screen'
    assert calls == ['lease', 'stop'] and child.poll() == 0
    journal.db.close.assert_called_once()
