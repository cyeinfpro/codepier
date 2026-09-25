import hashlib
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub.agent_install import AgentPackage, DeviceLifecycleInput, InstallTicketInput, install_command, make_agent_install_router, validate_root
from hub.auth import Auth
from hub.store import Store
from hub.runtime import Principal
from shared.crypto import digest, password_hash
from shared.util import DevError
from tests.install_support import package_tree, _real_auth_app




def test_install_arguments_are_strict_and_shell_quoted():
    with pytest.raises(Exception):
        validate_root('relative/path', 'posix')
    with pytest.raises(Exception):
        validate_root('C:\\Projects', 'posix')
    assert validate_root('C:\\Projects', 'windows') == 'C:\\Projects'
    assert "'$(touch pwned)'" in install_command('posix', 'https://hub.invalid', 'rdi_token', 'a' * 64, '$(touch pwned)')
    command = install_command('windows', 'https://hub.invalid', "rdi_'token", 'a' * 64, r'C:\Users\me')
    assert "rdi_''token" in command and '-AllowRoot' in command
    bootstrap = 'b' * 64
    posix_pinned = install_command('posix', 'https://hub.invalid', 'rdi_token', 'a' * 64, '/tmp', bootstrap)
    assert bootstrap in posix_pinned and 'sha256sum' in posix_pinned and 'shasum -a 256' in posix_pinned
    windows_pinned = install_command('windows', 'https://hub.invalid', 'rdi_token', 'a' * 64, r'C:\tmp', bootstrap)
    assert bootstrap in windows_pinned and 'Get-FileHash' in windows_pinned
    with pytest.raises(Exception):
        install_command('posix', 'https://hub.invalid', 'rdi_token', 'a' * 64, '/tmp', 'invalid')


def test_package_is_deterministic_and_excludes_private_files(tmp_path):
    package_tree(tmp_path)
    (tmp_path / 'config.json').write_text('SECRET=config\n')
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests' / 'secret.py').write_text('SECRET=test\n')
    first = AgentPackage(tmp_path).build()
    second = AgentPackage(tmp_path).build()
    assert first.content == second.content
    assert first.sha256 == hashlib.sha256(first.content).hexdigest()
    names = __import__('zipfile').ZipFile(__import__('io').BytesIO(first.content)).namelist()
    assert {'agent/lifecycle.py', 'shared/agent_lifecycle.py', 'scripts/install_agent.py',
            'scripts/agent_lifecycle.py'} <= set(names)
    assert 'scripts/helper.py' not in names
    assert 'config.json' not in names and 'tests/secret.py' not in names


def test_ticket_is_single_use_rotated_and_secret_never_in_command(tmp_path):
    package_tree(tmp_path / 'source')
    store = Store(tmp_path / 'hub')
    secret = 's' * 48
    device_id = 'd' * 32
    store.execute('INSERT INTO devices(id,name,secret,created) VALUES (?,?,?,?)',
                  (device_id, 'Laptop', store.encrypt(secret), 1))

    class Auth:
        def admin(self, request, write=False):
            return Principal('panel:admin', 'u', {'read', 'write', 'computer'}, ['*'], admin=True)

    runtime = SimpleNamespace(store=store)
    app = FastAPI()
    @app.exception_handler(DevError)
    async def dev_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'error': {'code': exc.code, 'message': exc.message}}, status_code=exc.status)
    app.include_router(make_agent_install_router(runtime, Auth(), tmp_path / 'source'))
    with TestClient(app) as client:
        ticket = client.post('/api/devices/'+device_id+'/install-ticket', json={
            'hub_url': 'https://hub.invalid/', 'platform': 'posix', 'allow_root': '/Users/me/Projects'})
        assert ticket.status_code == 200, ticket.text
        data = ticket.json()
        assert data['package']['version']
        assert secret not in data['command']
        posix_bootstrap_sha = hashlib.sha256((tmp_path / 'source/deploy/install-from-hub.sh').read_bytes()).hexdigest()
        assert posix_bootstrap_sha in data['command']
        token_match = re.search(r'--token\s+(rdi_[A-Za-z0-9_-]{43})', data['command'])
        assert token_match
        enrollment = client.post('/agent/enroll', headers={'Authorization': 'Bearer '+token_match.group(1)})
        assert enrollment.status_code == 200
        assert enrollment.json() == {'device_id': device_id, 'name': 'Laptop', 'secret': secret, 'hub_url': 'https://hub.invalid'}
        assert client.post('/agent/enroll', headers={'Authorization': 'Bearer '+token_match.group(1)}).status_code == 401
        # A second ticket supersedes the first, and the package is pinned by SHA.
        second = client.post('/api/devices/'+device_id+'/install-ticket', json={
            'hub_url': 'https://hub.invalid', 'platform': 'windows', 'allow_root': r'C:\Projects'})
        assert second.status_code == 200
        windows_bootstrap_sha = hashlib.sha256((tmp_path / 'source/deploy/install-from-hub.ps1').read_bytes()).hexdigest()
        assert windows_bootstrap_sha in second.json()['command']
        package_url = second.json()['package']['url']
        sha = parse_qs(urlsplit(package_url).query)['sha256'][0]
        downloaded = client.get(urlsplit(package_url).path+'?sha256='+sha)
        assert downloaded.status_code == 200 and hashlib.sha256(downloaded.content).hexdigest() == sha
        assert client.get(urlsplit(package_url).path+'?sha256='+'0'*64).status_code == 409
    store.close()




def test_install_ticket_requires_production_session_csrf_and_origin(tmp_path):
    app, store, device_id, _secret, cookie, csrf = _real_auth_app(tmp_path)
    body = {'hub_url': 'https://hub.invalid', 'platform': 'posix', 'allow_root': '/Users/me'}
    with TestClient(app) as client:
        assert client.post(f'/api/devices/{device_id}/install-ticket', json=body).status_code == 401
        client.cookies.set('rd_session', cookie)
        assert client.post(f'/api/devices/{device_id}/install-ticket', json=body).status_code == 403
        assert client.post(f'/api/devices/{device_id}/install-ticket', json=body,
                           headers={'X-RD-CSRF': csrf, 'Origin': 'https://evil.invalid'}).status_code == 403
        response = client.post(f'/api/devices/{device_id}/install-ticket', json=body,
                               headers={'X-RD-CSRF': csrf})
        assert response.status_code == 200, response.text
    store.close()


def test_install_ticket_rejects_expired_rotated_and_disabled_credentials(tmp_path):
    app, store, device_id, secret, cookie, csrf = _real_auth_app(tmp_path)
    body = {'hub_url': 'https://hub.invalid', 'platform': 'posix', 'allow_root': '/Users/me'}
    with TestClient(app) as client:
        client.cookies.set('rd_session', cookie)
        headers = {'X-RD-CSRF': csrf}
        first = client.post(f'/api/devices/{device_id}/install-ticket', json=body, headers=headers).json()
        first_token = re.search(r'--token\s+(rdi_[A-Za-z0-9_-]{43})', first['command']).group(1)
        store.execute('UPDATE agent_install_tickets SET expires=?', (time.time() - 1,))
        assert client.post('/agent/enroll', headers={'Authorization': 'Bearer ' + first_token}).status_code == 401
        second = client.post(f'/api/devices/{device_id}/install-ticket', json=body, headers=headers).json()
        second_token = re.search(r'--token\s+(rdi_[A-Za-z0-9_-]{43})', second['command']).group(1)
        assert second_token != first_token
        # A ticket rotated before redemption is no longer valid, while the new one is.
        third = client.post(f'/api/devices/{device_id}/install-ticket', json=body, headers=headers).json()
        third_token = re.search(r'--token\s+(rdi_[A-Za-z0-9_-]{43})', third['command']).group(1)
        assert client.post('/agent/enroll', headers={'Authorization': 'Bearer ' + second_token}).status_code == 401
        store.execute('UPDATE devices SET enabled=0 WHERE id=?', (device_id,))
        assert client.post('/agent/enroll', headers={'Authorization': 'Bearer ' + third_token}).status_code == 401
        store.execute('UPDATE devices SET enabled=1,secret=? WHERE id=?', (store.encrypt(secret + 'rotated'), device_id))
        assert client.post('/agent/enroll', headers={'Authorization': 'Bearer ' + third_token}).status_code == 401
    store.close()


def test_agent_lifecycle_endpoints_pin_package_and_require_exact_uninstall_name(tmp_path):
    source = tmp_path / 'source'
    package_tree(source)
    store = Store(tmp_path / 'hub')
    device_id = 'd' * 32
    store.execute('INSERT INTO devices(id,name,secret,enabled,created) VALUES (?,?,?,?,?)',
                  (device_id, 'Studio Mac', store.encrypt('s' * 48), 1, time.time()))
    calls = []

    class AdminAuth:
        def admin(self, request, write=False):
            return Principal('panel:admin', 'u', {'read', 'write', 'computer'}, ['*'], admin=True)

    class Runtime:
        def __init__(self):
            self.store = store

        async def dispatch_device_action(self, action, args, target, principal):
            calls.append((action, args, target, principal.admin))
            return {'operation_id': f'op-{len(calls)}', 'pending': True, 'state': 'queued'}

    app = FastAPI()

    @app.exception_handler(DevError)
    async def dev_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'error': {'code': exc.code, 'message': exc.message}}, status_code=exc.status)

    app.include_router(make_agent_install_router(Runtime(), AdminAuth(), source))
    with TestClient(app) as client:
        body = {'idempotency_key': 'lifecycle-update-001', 'confirmation': ''}
        update = client.post(f'/api/devices/{device_id}/agent-update', json=body)
        assert update.status_code == 200, update.text
        action, args, target, admin = calls[-1]
        package = AgentPackage(source).build()
        assert (action, target, admin) == ('agent_update', device_id, True)
        assert set(args) == {'idempotency_key', 'package_sha256', 'package_bytes', 'target_version'}
        assert args['idempotency_key'] == body['idempotency_key']
        assert args['package_sha256'] == package.sha256
        assert args['package_bytes'] == len(package.content)
        assert args['target_version']

        restart = client.post(f'/api/devices/{device_id}/agent-restart', json={
            'idempotency_key': 'lifecycle-restart-001', 'confirmation': ''})
        assert restart.status_code == 200
        assert calls[-1][0] == 'agent_restart'
        assert calls[-1][1] == {'idempotency_key': 'lifecycle-restart-001'}

        wrong = client.post(f'/api/devices/{device_id}/agent-uninstall', json={
            'idempotency_key': 'lifecycle-uninstall-001', 'confirmation': 'studio mac'})
        assert wrong.status_code == 409
        assert len(calls) == 2
        correct = client.post(f'/api/devices/{device_id}/agent-uninstall', json={
            'idempotency_key': 'lifecycle-uninstall-001', 'confirmation': 'Studio Mac'})
        assert correct.status_code == 200 and calls[-1][0] == 'agent_uninstall'

        extra = client.post(f'/api/devices/{device_id}/agent-restart', json={
            'idempotency_key': 'lifecycle-restart-002', 'confirmation': '', 'unexpected': True})
        assert extra.status_code == 422
    store.close()
