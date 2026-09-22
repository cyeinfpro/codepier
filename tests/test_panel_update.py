"""Update trust boundaries, durable state machine, real UDS/Auth and maintenance gates."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import zipfile

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from hub.auth import Auth
from hub.panel_update import UpdaterClient, make_panel_update_router
from hub.store import Store
from scripts import panel_update_source as source
from scripts import panel_updater as updater
from scripts.panel_update_runtime import COPY_DATA, DockerRuntime, atomic_json, fingerprint, read_json, save_compose, updated_env
from shared.crypto import digest
from shared.panel_maintenance import PanelMaintenance, PanelMaintenanceMiddleware
from shared.util import DevError, VERSION

_CURRENT = tuple(int(part) for part in VERSION.split('.'))
TARGET = f'{_CURRENT[0]}.{_CURRENT[1] + 1}.0'


def bundle_bytes(*, change=None, extra=None, bad_manifest=False):
    files = {name: b'# fixture\n' for name in source.REQUIRED}
    files['shared/util.py'] = f'VERSION = "{TARGET}"\n'.encode()
    files['RELEASE.json'] = json.dumps({'name': 'CodePier', 'version': TARGET}).encode()
    if change:
        change(files)
    manifest = ''.join(hashlib.sha256(raw).hexdigest() + '  ' + name + '\n' for name, raw in sorted(files.items()))
    if bad_manifest:
        manifest = manifest.replace(manifest[:64], '0' * 64, 1)
    files['MANIFEST.sha256'] = manifest.encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for name, raw in files.items():
            entry = zipfile.ZipInfo(name)
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            z.writestr(entry, raw)
        if extra:
            z.writestr(*extra)
    return stream.getvalue()


def release_for(raw):
    return {'repository': source.DEFAULT_REPOSITORY, 'release_id': 21, 'asset_id': 31,
            'version': TARGET, 'tag': 'v' + TARGET, 'sha256': hashlib.sha256(raw).hexdigest(),
            'bytes': len(raw), 'url': f'https://github.com/cyeinfpro/codepier/releases/download/v{TARGET}/codepier-{TARGET}-source.zip',
            'notes': '<img src=x onerror=alert(1)>', 'published_at': '2026-09-22T00:00:00Z', 'checked_at': time.time()}


def test_verified_bundle_and_runtime_version(tmp_path):
    raw = bundle_bytes(); archive = tmp_path / 'source.zip'; archive.write_bytes(raw)
    root = source.unpack_bundle(archive, tmp_path / 'source', release_for(raw))
    assert source.source_version(root) == TARGET
    assert (root / 'hub/panel_update.py').is_file()
    with pytest.raises(source.UpdateError, match='已经存在'):
        source.unpack_bundle(archive, root, release_for(raw))


@pytest.mark.parametrize('name', ['../outside', '/etc/passwd', 'hub/../../oops', 'hub\\oops.py',
    'hub//oops.py', 'hub/./oops.py', '.env', 'config.json', 'hub/config.json', '.git/config',
    'hub/a:b', 'hub/\x00bad', 'unknown/file.py'])
def test_unsafe_paths_rejected(name):
    with pytest.raises(source.UpdateError):
        source.safe_name(name)


@pytest.mark.parametrize('mutation', ['sha', 'size', 'manifest', 'extra', 'symlink', 'duplicate', 'version', 'missing', 'file_limit', 'total_limit'])
def test_invalid_archive_never_installs(tmp_path, monkeypatch, mutation):
    extra = None; change = None
    if mutation == 'extra': extra = ('hub/unchecked.py', 'unchecked')
    if mutation == 'duplicate': extra = ('hub/app.py', 'duplicate')
    if mutation == 'symlink':
        entry = zipfile.ZipInfo('hub/link.py'); entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        extra = (entry, '../../outside')
    if mutation == 'version': change = lambda files: files.update({'RELEASE.json': b'{"name":"CodePier","version":"0.0.1"}'})
    if mutation == 'missing': change = lambda files: files.pop('hub/app.py')
    raw = bundle_bytes(change=change, extra=extra, bad_manifest=mutation == 'manifest')
    archive = tmp_path / 'source.zip'; archive.write_bytes(raw); release = release_for(raw)
    if mutation == 'sha': release['sha256'] = '0' * 64
    if mutation == 'size': release['bytes'] += 1
    if mutation == 'file_limit': monkeypatch.setattr(source, 'MAX_FILE', 10)
    if mutation == 'total_limit': monkeypatch.setattr(source, 'MAX_FILES', 2)
    with pytest.raises(source.UpdateError):
        source.unpack_bundle(archive, tmp_path / 'candidate', release)
    assert not (tmp_path / 'candidate').exists()


def github_metadata(raw):
    r = release_for(raw)
    return {'id': r['release_id'], 'tag_name': r['tag'], 'draft': False, 'prerelease': False,
            'body': r['notes'], 'published_at': r['published_at'], 'assets': [{
                'id': r['asset_id'], 'name': f'codepier-{TARGET}-source.zip', 'state': 'uploaded',
                'size': r['bytes'], 'digest': 'sha256:' + r['sha256'], 'browser_download_url': r['url']}]}


@pytest.mark.parametrize('mutation', ['ok', 'draft', 'prerelease', 'digest', 'url', 'size', 'missing', 'duplicate', 'tag'])
def test_only_exact_stable_release_asset(monkeypatch, mutation):
    data = github_metadata(bundle_bytes())
    if mutation in {'draft', 'prerelease'}: data[mutation] = True
    if mutation == 'digest': data['assets'][0].pop('digest')
    if mutation == 'url': data['assets'][0]['browser_download_url'] = 'https://evil.invalid/code.zip'
    if mutation == 'size': data['assets'][0]['size'] = True
    if mutation == 'missing': data['assets'] = []
    if mutation == 'duplicate': data['assets'] *= 2
    if mutation == 'tag': data['tag_name'] = 'v1.11.0-beta'
    monkeypatch.setattr(source, 'fetch', lambda *a, **kw: json.dumps(data).encode())
    if mutation == 'ok':
        assert source.latest_release()['version'] == TARGET
    else:
        with pytest.raises(source.UpdateError): source.latest_release()


@pytest.mark.parametrize('url', ['http://github.com/x', 'https://localhost/x', 'https://github.com.evil/x', 'https://user@github.com/x', 'https://github.com:444/x'])
def test_untrusted_download_origin_rejected(url):
    with pytest.raises(source.UpdateError): source.fetch(url, limit=10)


class FakeRuntime:
    def __init__(self, home, fail=''):
        self.home, self.fail, self.calls = home, fail, []
        self.gate = home / 'run/maintenance.json'
    def action(self, name):
        self.calls.append(name)
        if self.fail == name: raise source.UpdateError('INJECTED', 'injected ' + name, 500)
    def set_gate(self, job):
        self.action('set_gate'); atomic_json(self.gate, {'id': job['id']})
    def clear_gate(self):
        self.action('clear_gate'); self.gate.unlink(missing_ok=True)
    def prepare(self, job, root):
        self.action('prepare'); return {'old_volume': 'original', 'new_volume': 'candidate', 'image': 'fixture-image', 'source': str(root)}
    def build(self, job, root):
        self.action('build'); return {'agent_sha256': 'a' * 64, 'agent_bytes': 123, 'version': TARGET}
    def quiesce(self, job):
        self.set_gate(job); self.action('quiesce')
    def stop(self, job): self.action('stop')
    def copy_data(self, job): self.action('copy_data'); return {'integrity': 'ok'}
    def start(self, job, which): self.action('start_' + which)
    def verify(self, job): self.action('verify')
    def commit(self, job):
        self.action('commit')
        current = read_json(self.home / 'current.json'); current['version'] = TARGET
        atomic_json(self.home / 'current.json', current)
    def rollback(self, job):
        self.action('rollback'); self.gate.unlink(missing_ok=True)
        atomic_json(self.home / 'current.json', read_json(self.home / 'jobs' / job['id'] / 'current.before.json'))


def manager_fixture(root, monkeypatch=None, fail=''):
    home = root / '.codepier-updater'; home.mkdir(parents=True)
    atomic_json(home / 'config.json', {'repository': source.DEFAULT_REPOSITORY, 'project': 'codepier'})
    atomic_json(home / 'current.json', {'version': VERSION, 'compose': {}, 'files': {}})
    raw = bundle_bytes(); release = release_for(raw)
    atomic_json(home / 'candidate.json', release)
    runtime = FakeRuntime(home, fail)
    manager = updater.Manager(root, runtime=runtime, start_workers=False)
    if monkeypatch:
        monkeypatch.setattr(updater, 'latest_release', lambda *a: copy.deepcopy(release))
        monkeypatch.setattr(updater, 'fetch', lambda url, **kw: Path(kw['destination']).write_bytes(raw))
    return manager, release


def body_for(release=None, key='fixture-key-123'):
    body = {'idempotency_key': key, 'current_version': VERSION, 'actor': 'panel:admin'}
    if release: body.update({k: release[k] for k in ('version', 'release_id', 'sha256')})
    return body


def perform(manager, release):
    result = manager.submit('apply', body_for(release)); manager.perform(manager.current_job())
    return manager.status()['operation']


def test_success_replay_no_remote_agent_restart_and_persisted_status(tmp_path, monkeypatch):
    manager, release = manager_fixture(tmp_path, monkeypatch)
    body = body_for(release)
    first = manager.submit('apply', body)
    assert manager.submit('apply', body)['replayed']
    manager.perform(manager.current_job())
    job = manager.status()['operation']
    assert job['state'] == 'succeeded' and job['id'] == first['operation']['id']
    assert manager.runtime.calls == ['prepare','build','set_gate','quiesce','stop','copy_data','start_target','verify','commit','clear_gate']
    assert not manager.runtime.gate.exists()
    assert not (tmp_path / '.codepier-install.lock').exists()
    reboot = updater.Manager(tmp_path, runtime=manager.runtime, start_workers=False)
    assert reboot.status(body['idempotency_key'])['request_found'] is True
    assert reboot.submit('apply', body)['replayed']
    assert 'actor' not in job and 'source' not in job and 'compose' not in job


@pytest.mark.parametrize('failure,expected', [('prepare','failed'),('build','failed'),('quiesce','failed'),
    ('stop','rolled_back'),('copy_data','rolled_back'),('start_target','rolled_back'),('verify','rolled_back'),
    ('commit','rolled_back'),('clear_gate','recovery_required')])
def test_failure_boundaries_and_no_stale_data_rollback(tmp_path, monkeypatch, failure, expected):
    manager, release = manager_fixture(tmp_path, monkeypatch, failure)
    job = perform(manager, release)
    assert job['state'] == expected, job
    assert ('rollback' in manager.runtime.calls) is (expected == 'rolled_back')
    if expected != 'recovery_required':
        assert not manager.runtime.gate.exists()
        assert not (tmp_path / '.codepier-install.lock').exists()
    else:
        assert job['commit_decided'] and 'rollback' not in manager.runtime.calls
        assert (tmp_path / '.codepier-install.lock').exists()


@pytest.mark.parametrize('phase', ['queued','downloading','building','quiescing','stopping','copying','starting','verifying','committing','committed'])
def test_restart_does_not_replay_and_preserves_commit_decision(tmp_path, phase):
    manager, release = manager_fixture(tmp_path)
    manager.submit('apply', body_for(release)); job = manager.current_job()
    manager.acquire_install_lock(job)
    manager.save(job, phase=phase, commit_decided=phase == 'committed')
    manager.runtime.set_gate(job) if phase not in {'queued','downloading','building'} else None
    manager.runtime.calls.clear(); manager.recover()
    state = manager.status()['operation']['state']
    if phase == 'committed':
        assert state == 'succeeded'
        assert manager.runtime.calls == ['set_gate','verify','clear_gate']
    elif phase in updater.CUTOVER_PHASES:
        assert state == 'rolled_back' and manager.runtime.calls == ['rollback']
    else:
        assert state == 'failed' and 'build' not in manager.runtime.calls
    assert not (tmp_path / '.codepier-install.lock').exists()


def test_concurrency_mismatch_expiration_and_manual_install_lock(tmp_path, monkeypatch):
    manager, release = manager_fixture(tmp_path, monkeypatch)
    body = body_for(release); manager.submit('apply', body)
    with pytest.raises(source.UpdateError) as e: manager.submit('apply', {**body, 'actor':'panel:other'})
    assert e.value.code == 'IDEMPOTENCY_CONFLICT'
    with pytest.raises(source.UpdateError) as e: manager.submit('check', body_for(key='other-check-key'))
    assert e.value.code == 'UPDATE_BUSY'
    lock = tmp_path / '.codepier-install.lock'; lock.mkdir(); (lock/'pid').write_text('other-owner')
    manager.perform(manager.current_job())
    assert manager.status()['operation']['error']['code'] == 'INSTALL_BUSY'
    assert (lock/'pid').read_text() == 'other-owner'
    assert not manager.runtime.calls
    release['checked_at'] = time.time() - 1000; atomic_json(manager.home/'candidate.json',release)
    with pytest.raises(source.UpdateError) as e: manager.submit('apply', body_for(release,key='expired-apply-key'))
    assert e.value.code == 'RELEASE_EXPIRED'


def test_release_changed_after_confirmation_leaves_runtime_untouched(tmp_path, monkeypatch):
    manager, release = manager_fixture(tmp_path, monkeypatch)
    manager.submit('apply', body_for(release))
    monkeypatch.setattr(updater,'latest_release',lambda *a:{**release,'sha256':'0'*64})
    manager.perform(manager.current_job())
    assert manager.status()['operation']['error']['code'] == 'RELEASE_CHANGED'
    assert manager.runtime.calls == []


def test_literal_dollars_and_environment_preservation(tmp_path):
    spec={'services':{'hub':{'environment':{'TOKEN':'a$B${SECRET}$$','HUB_PORT':'8765'},'command':['echo','$HOME']}}}
    save_compose(tmp_path/'target.json',spec)
    assert read_json(tmp_path/'target.spec.json') == spec
    assert read_json(tmp_path/'target.json')['services']['hub']['environment']['TOKEN'] == 'a$$B$${SECRET}$$$$'
    before=b'# keep\nHUB_PUBLIC_URL=https://host.example\nexport CODEPIER_HUB_IMAGE=old\nCODEPIER_HUB_IMAGE=duplicate\nSECRET=\'a$b\'\n'
    after=updated_env(before,{'CODEPIER_HUB_IMAGE':'new','CODEPIER_HUB_DATA_VOLUME':'new-data'})
    assert b"SECRET='a$b'" in after and b'https://host.example' in after
    assert after.count(b'CODEPIER_HUB_IMAGE=')==1 and b'CODEPIER_HUB_IMAGE=new' in after


def test_real_data_copy_preserves_identity_database_and_files(tmp_path):
    original=tmp_path/'original'; target=tmp_path/'target';original.mkdir();target.mkdir()
    (original/'master.key').write_text('fixture-key'); (original/'master.key').chmod(0o600)
    (original/'uploads').mkdir(mode=0o700);(original/'uploads/file.txt').write_text('attachments')
    (original/'.hub.lock').write_text('not copied')
    with sqlite3.connect(original/'hub.sqlite3') as db:
        db.execute('create table users(id text)');db.execute("insert into users values ('preserved')")
    script=COPY_DATA.replace("Path('/backup'),Path('/app/data')",f'Path({str(original)!r}),Path({str(target)!r})')
    script=script.replace('file:/app/data/hub.sqlite3?mode=ro',(target/'hub.sqlite3').as_uri()+'?mode=ro')
    result=subprocess.run([os.sys.executable,'-c',script],text=True,capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['integrity']=='ok'
    assert fingerprint(original/'master.key')==fingerprint(target/'master.key')
    assert not (target/'.hub.lock').exists()
    assert (target/'uploads/file.txt').read_text()=='attachments'
    assert stat.S_IMODE((target/'uploads').stat().st_mode)==0o700
    assert fingerprint(original/'hub.sqlite3')==fingerprint(target/'hub.sqlite3')


def make_auth_app(root, client):
    store=Store(root/'hub')
    store.execute('INSERT INTO users(id,username,password_hash,created) VALUES (?,?,?,?)',('u1','admin','unused',time.time()))
    store.execute('INSERT INTO sessions(id_hash,user_id,csrf,expires) VALUES (?,?,?,?)',(digest('cookie'),'u1','csrf',time.time()+3600))
    app=FastAPI();runtime=SimpleNamespace(store=store)
    @app.exception_handler(DevError)
    async def error(request,exc): return JSONResponse({'error':{'code':exc.code,'message':exc.message}},status_code=exc.status)
    app.include_router(make_panel_update_router(Auth(store),runtime,client))
    return app,store


def test_real_unix_socket_bridge_admin_csrf_origin_and_idempotency(tmp_path):
    manager,release=manager_fixture(tmp_path)
    with tempfile.TemporaryDirectory(prefix='cp-update-',dir='/tmp') as directory:
        socket=Path(directory)/'u.sock'
        with updater.Server(str(socket),updater.Handler) as server:
            server.manager=manager;thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            app,store=make_auth_app(tmp_path,UpdaterClient(str(socket)))
            try:
                with TestClient(app) as c:
                    body={'idempotency_key':'socket-check-123'}
                    assert c.get('/api/panel-update/status').status_code==401
                    assert c.post('/api/panel-update/check',json=body).status_code==401
                    c.cookies.set('rd_session','cookie')
                    assert c.post('/api/panel-update/check',json=body).status_code==403
                    assert c.post('/api/panel-update/check',json=body,headers={'X-RD-CSRF':'csrf','Origin':'https://evil.invalid'}).status_code==403
                    response=c.post('/api/panel-update/check',json=body,headers={'X-RD-CSRF':'csrf'})
                    assert response.status_code==202,response.text
                    assert c.post('/api/panel-update/check',json=body,headers={'X-RD-CSRF':'csrf'}).json()['replayed']
                    status=c.get('/api/panel-update/status',params={'request_key':body['idempotency_key']}).json()
                    assert status['request_found'] and status['operation']['state']=='queued'
                    assert c.post('/api/panel-update/check',json={**body,'url':'https://evil.invalid'},headers={'X-RD-CSRF':'csrf'}).status_code==422
                    apply={k:release[k] for k in ('version','release_id','sha256')}
                    apply.update(idempotency_key='socket-apply-123',confirmation='wrong')
                    assert c.post('/api/panel-update/apply',json=apply,headers={'X-RD-CSRF':'csrf'}).status_code==409
                    assert c.get('/api/panel-update/status',params={'request_key':'../bad'}).status_code==422
            finally:
                server.shutdown();thread.join(timeout=3);store.close()


def test_missing_updater_is_explicitly_disabled_not_a_completed_update(tmp_path):
    app,store=make_auth_app(tmp_path,UpdaterClient(''))
    try:
        with TestClient(app) as c:
            c.cookies.set('rd_session','cookie')
            data=c.get('/api/panel-update/status').json()
            assert not data['enabled'] and data['code']=='UPDATER_NOT_CONFIGURED'
            assert 'operation' not in data
    finally:store.close()


def test_production_hub_maintenance_and_health(tmp_path,monkeypatch):
    from hub.app import create_app
    gate_dir=tmp_path/'run';gate_dir.mkdir()
    monkeypatch.setenv('HUB_PANEL_UPDATE_SOCKET',str(gate_dir/'updater.sock'))
    app=create_app(str(tmp_path/'real-hub'))
    with TestClient(app) as client:
        assert client.get('/healthz').json()['panel_update']=={'maintenance':False,'ready':False}
        (gate_dir/'maintenance.json').write_text('{}')
        result=client.get('/healthz')
        assert result.status_code==200 and result.json()['panel_update']['ready']
        assert client.post('/api/login',json={'username':'admin','password':'example'}).status_code==503
        assert client.get('/oauth/authorize').status_code==503
        assert client.get('/api/panel-update/status').status_code==401
        assert client.get('/agent/manifest.json').status_code==200
        assert client.get('/static/panel-update.js').status_code==200
        with pytest.raises(Exception):
            with client.websocket_connect('/agent/ws/not-a-device'): pass
        with pytest.raises(DevError): app.state.runtime.panel_maintenance.guard()
        app.state.runtime.panel_maintenance.inflight=1
        assert not client.get('/healthz').json()['panel_update']['ready']
        app.state.runtime.panel_maintenance.inflight=0
        (gate_dir/'maintenance.json').unlink()
        assert client.get('/healthz').json()['panel_update']['maintenance'] is False
