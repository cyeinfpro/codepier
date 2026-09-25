#!/usr/bin/env python3
"""Real isolated Authentik + Chromium + CodePier acceptance; no live credentials.

Starts its own uniquely named Compose project on a loopback port and temporary
Hub. Creates only disposable users/provider/resources. Never connects to a
configured production IdP. Reports contain checks, not passwords/codes/Tokens.
Requires Docker Compose, locked requirements-dev, and installed Chromium.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shared.role_contracts import ROLE_SCOPE


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def required(response, status=200):
    if response.status_code != status:
        # Never echo a provider/token response body or callback query.
        raise AssertionError(f'{response.request.method} {response.request.url.path}: expected {status}, got {response.status_code}')
    return response.json() if response.content else {}


def await_ready(predicate, seconds=300):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (httpx.HTTPError, KeyError, ValueError):
            pass
        time.sleep(2)
    raise RuntimeError('Disposable service readiness deadline exceeded')


def password_hash(password):
    salt = secrets.token_hex(12)
    count = 1000000
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), count)
    return f'pbkdf2_sha256${count}${salt}${base64.b64encode(key).decode()}'


def acceptance(output):
    result = {'provider_version': '2026.8.3', 'production_changed': False,
              'checks': [], 'status': 'running'}
    phase = 'startup'
    secret_values = []
    def secret():
        value = secrets.token_urlsafe(36)
        secret_values.append(value)
        if os.getenv('GITHUB_ACTIONS') == 'true':
            print('::add-mask::' + value, flush=True)
        return value
    def passed(name):
        result['checks'].append(name)
        print('PASS ' + name, flush=True)
    with tempfile.TemporaryDirectory(prefix='codepier-oidc-acceptance-') as temporary:
        folder = Path(temporary)
        env = {**os.environ, 'TEST_DB_PASSWORD': secret(), 'TEST_AUTHENTIK_SECRET': secret(),
               'TEST_BOOTSTRAP_TOKEN': secret(), 'TEST_BOOTSTRAP_HASH': password_hash(secret()),
               'TEST_AUTHENTIK_PORT': str(free_port())}
        project = 'codepier-oidc-test-' + uuid.uuid4().hex[:12]
        compose = ['docker', 'compose', '-f', str(ROOT/'tests/fixtures/oidc-authentik/compose.yml'), '-p', project]
        def docker(*args, check=True):
            return subprocess.run([*compose, *args], env=env, capture_output=True, text=True,
                                  timeout=360, check=check)
        hub = None
        logs = (folder/'services.log').open('w+')
        try:
            phase = 'authentik-bootstrap'
            docker('up', '-d')
            issuer_base = 'http://127.0.0.1:' + env['TEST_AUTHENTIK_PORT']
            ak = httpx.Client(base_url=issuer_base, headers={'Authorization': 'Bearer ' + env['TEST_BOOTSTRAP_TOKEN']}, timeout=20)
            await_ready(lambda: ak.get('/api/v3/core/users/me/').status_code == 200)
            def ak_call(method, path, body=None, status=200):
                return required(ak.request(method, '/api/v3/' + path, json=body), status)
            def one(path, field, value):
                entries = ak_call('GET', path)['results']
                return next(x for x in entries if x[field] == value)
            authorization = one('flows/instances/?page_size=100', 'slug', 'default-provider-authorization-implicit-consent')['pk']
            invalidation = one('flows/instances/?page_size=100', 'slug', 'default-provider-invalidation-flow')['pk']
            authentication = one('flows/instances/?page_size=100', 'slug', 'default-authentication-flow')['pk']
            keys = ak_call('GET', 'crypto/certificatekeypairs/?page_size=100')['results']
            signing = next(x['pk'] for x in keys if x.get('private_key_available'))
            mappings = ak_call('GET', 'propertymappings/provider/scope/?page_size=100')['results']
            scopes = [x['pk'] for x in mappings if x.get('scope_name') in ('openid', 'profile', 'email', 'offline_access')]
            group = ak_call('POST', 'core/groups/', {'name': 'codepier-acceptance-team'}, 201)
            users = []
            for name in ('alice', 'bob'):
                password = secret()
                u = ak_call('POST', 'core/users/', {'username': 'codepier-test-' + name, 'name': name.title(),
                            'is_active': True, 'groups': [group['pk']], 'email': name+'@example.test'}, 201)
                ak_call('POST', f"core/users/{u['pk']}/set_password/", {'password': password}, 204)
                users.append((u, password))
            passed('isolated_provider_and_two_ordinary_users')
            phase = 'hub-bootstrap'
            hub_port = free_port()
            hub_url = f'http://127.0.0.1:{hub_port}'
            hub_dir = folder/'hub'
            hub_env = {**os.environ, 'CODEPIER_ADMIN_PASSWORD': secret(), 'HUB_PUBLIC_URL': hub_url,
                       'MCP_PUBLIC_URL': hub_url, 'CODEPIER_OIDC_INSECURE_TEST_LOOPBACK': '1'}
            subprocess.run([sys.executable, '-m', 'hub', '--data-dir', str(hub_dir), 'init'], cwd=ROOT,
                           env=hub_env, stdout=logs, stderr=logs, check=True, timeout=30)
            hub = subprocess.Popen([sys.executable, '-m', 'hub', '--data-dir', str(hub_dir), 'run', '--host',
                                    '127.0.0.1', '--port', str(hub_port)], cwd=ROOT, env=hub_env, stdout=logs, stderr=logs)
            owner = httpx.Client(base_url=hub_url, timeout=30)
            await_ready(lambda: owner.get('/healthz').status_code == 200, 60)
            login = required(owner.post('/api/login', json={'username': 'admin', 'password': hub_env['CODEPIER_ADMIN_PASSWORD']}))
            owner.headers.update({'X-RD-CSRF': login['csrf'], 'X-CodePier-Space': 'legacy'})
            sid = required(owner.post('/api/iam/spaces', json={'label': 'Provider acceptance team', 'idempotency_key': uuid.uuid4().hex}), 201)['id']
            owner.headers['X-CodePier-Space'] = sid
            role = required(owner.post('/api/access-roles', json={'label': 'secretary',
                            'project_rules': [{'actions': ['read'], 'all_projects': True}],
                            'device_rules': [], 'idempotency_key': uuid.uuid4().hex}), 201)
            client_id, client_secret = 'codepier-acceptance', secret()
            provider_config = {'label': 'Isolated Authentik', 'issuer': issuer_base+'/application/o/codepier-test/',
                               'client_id': client_id, 'client_secret': client_secret, 'enabled': True,
                               'admission': 'jit', 'scopes': 'openid profile email offline_access', 'freshness_seconds': 300}
            provider = required(owner.post('/api/iam/oidc/providers', json=provider_config), 201)
            callback = hub_url + '/auth/oidc/' + provider['id'] + '/callback'
            ak_provider = ak_call('POST', 'providers/oauth2/', {
                'name': 'CodePier isolated test', 'authorization_flow': authorization, 'authentication_flow': authentication,
                'invalidation_flow': invalidation, 'client_type': 'confidential', 'client_id': client_id,
                'client_secret': client_secret, 'signing_key': signing, 'include_claims_in_id_token': True,
                'redirect_uris': [{'matching_mode': 'strict', 'url': callback}], 'property_mappings': scopes,
            }, 201)
            ak_call('POST', 'core/applications/', {'name': 'CodePier isolated test', 'slug': 'codepier-test', 'provider': ak_provider['pk']}, 201)
            verified = required(owner.post('/api/iam/oidc/providers/'+provider['id']+'/check'))
            assert verified['issuer'] == provider_config['issuer'] and verified['callback'] == callback
            required(owner.post('/api/iam/oidc/providers/'+provider['id']+'/mappings', json={
                'group_name': group['name'], 'space_id': sid, 'level': 'member', 'role_id': role['id'], 'may_delegate': True}), 201)
            passed('actual_discovery_jwks_and_exact_callback')
            phase = 'real-browser-login'
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                human_clients, identities, profiles = [], [], []
                for user, password in users:
                    context = browser.new_context()
                    page = context.new_page()
                    page.set_default_timeout(30000)
                    page.goto(hub_url)
                    page.locator('#oidc-login-buttons a').click()
                    page.locator('input[name="uidField"]').fill(user['username'])
                    page.locator('button[type="submit"]').click()
                    page.locator('input[name="password"]').fill(password)
                    page.locator('button[type="submit"]').click()
                    page.wait_for_url(hub_url+'/**', timeout=60000)
                    expect(page.locator('#iam-active-space')).to_be_visible(timeout=30000)
                    cookies = {c['name']: c['value'] for c in context.cookies(hub_url)}
                    client = httpx.Client(base_url=hub_url, cookies=cookies, timeout=30)
                    session = required(client.get('/api/session'))
                    client.headers.update({'X-RD-CSRF': session['csrf'], 'X-CodePier-Space': sid})
                    me = required(client.get('/api/iam/me'))
                    assert not me['instance_admin'] and not me['local_login']
                    assert {s['kind'] for s in me['spaces']} == {'personal', 'team'}
                    assert all(s['id'] != 'legacy' for s in me['spaces'])
                    assert client.get('/api/iam/users').status_code == 403
                    p = required(client.post('/api/access-profiles', json={'label': user['name']+' secretary',
                        'role_id': role['id'], 'idempotency_key': uuid.uuid4().hex}), 201)
                    human_clients.append(client); identities.append(me); profiles.append(p)
                    context.close()
                assert identities[0]['id'] != identities[1]['id']
                assert human_clients[0].get('/api/access-profiles/'+profiles[1]['id']).status_code in (403,404)
                passed('real_code_pkce_login_group_assignment_private_profiles')
                phase = 'downstream-role-consent'
                alice = human_clients[0]
                registration = required(alice.post('/oauth/register', json={'redirect_uris': ['http://localhost:19876/callback']}), 201)
                verifier = secrets.token_urlsafe(64)
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
                response = alice.get('/oauth/authorize', params={'response_type':'code','client_id':registration['client_id'],
                    'redirect_uri':'http://localhost:19876/callback','scope':ROLE_SCOPE,'resource':hub_url+'/mcp',
                    'code_challenge_method':'S256','code_challenge':challenge})
                assert response.status_code == 303
                auth_path = response.headers['location']
                ctx = browser.new_context()
                ctx.add_cookies([{'name': 'rd_session', 'value': alice.cookies.get('rd_session'), 'url': hub_url}])
                page = ctx.new_page()
                page.route('http://localhost:19876/callback*', lambda route: route.fulfill(body='Authorized isolated test callback'))
                # Choose the team BEFORE the consent page creates its server-side binding.
                page.goto(hub_url)
                page.locator('#iam-active-space').select_option(sid)
                page.wait_for_function('(sid)=>S.space_id===sid', arg=sid)
                page.goto(hub_url+auth_path)
                page.select_option('#access-profile-selector', profiles[0]['id'])
                confirmation = page.locator('[name="confirm_dynamic_role"]')
                expect(confirmation).not_to_be_checked()
                confirmation.check(); page.locator('#allow-consent').click()
                page.wait_for_url('http://localhost:19876/callback*')
                code = parse_qs(urlsplit(page.url).query)['code'][0]
                tokens = required(alice.post('/oauth/token', data={'grant_type':'authorization_code','code':code,
                    'client_id':registration['client_id'],'redirect_uri':'http://localhost:19876/callback','code_verifier':verifier}))
                def rpc(name, token_value=tokens['access_token']):
                    return alice.post('/mcp', headers={'Authorization':'Bearer '+token_value},
                        json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':{}}})
                assert required(rpc('get_profile'))['result']['structuredContent']['id'] == profiles[0]['id']
                passed('separate_codepier_oauth_issuer_and_explicit_dynamic_role_consent')
                phase = 'dynamic-resource-and-refresh'
                # Seed an offline fixture mapping, not a real directory or credential.
                db = sqlite3.connect(hub_dir/'hub.sqlite3', timeout=30)
                with db:
                    device = 'fixture-'+uuid.uuid4().hex
                    db.execute('INSERT INTO devices(id,name,secret,space_id,owner_user_id,created) VALUES(?,?,?,?,?,?)',
                               (device,'offline fixture','not-a-live-credential',sid,identities[0]['id'],time.time()))
                    project_id = 'fixture-'+uuid.uuid4().hex
                    db.execute('INSERT INTO projects(id,alias,alias_key,device_id,root,space_id,owner_user_id,created) VALUES(?,?,?,?,?,?,?,?)',
                               (project_id,'new-project','new-project',device,str(folder/'unmapped'),sid,identities[0]['id'],time.time()))
                assert project_id in {p['id'] for p in required(rpc('projects_list'))['result']['structuredContent']['projects']}
                fresh = required(alice.post('/oauth/token', data={'grant_type':'refresh_token','refresh_token':tokens['refresh_token'],
                    'client_id':registration['client_id']}))
                assert fresh['refresh_token'] != tokens['refresh_token']
                assert required(rpc('get_profile', fresh['access_token']))['result']['structuredContent']['id'] == profiles[0]['id']
                passed('same_role_connection_gains_new_project_and_refresh_preserves_identity')
                phase = 'real-group-removal'
                ak_call('PATCH', f"core/users/{users[0][0]['pk']}/", {'groups': []})
                with db:
                    # Advance only the fixture's reconciliation schedule, not its policy/results.
                    db.execute('UPDATE external_identities SET checked_at=0 WHERE user_id=?',(identities[0]['id'],))
                required(owner.post('/api/iam/oidc/reconcile'))
                assert rpc('projects_list').status_code == 403
                assert all(s['id'] != sid for s in required(alice.get('/api/iam/me'))['spaces'])
                assert any(s['id'] == sid for s in required(human_clients[1].get('/api/iam/me'))['spaces'])
                passed('actual_idp_group_removal_revokes_team_and_existing_grant_only_for_target_user')
                assert db.execute('PRAGMA foreign_key_check').fetchall() == []
                db.close();ctx.close();browser.close()
                for client in human_clients:client.close()
            owner.close();ak.close()
            result['status']='passed'
        except Exception as exc:
            result['status']='failed';result['phase']=phase
            # Only type and sanitized bounded text; no raw provider responses/logs.
            detail=str(exc)
            for value in secret_values:detail=detail.replace(value,'[redacted]')
            result['error_type']=type(exc).__name__
            result['error']=re.sub(r'(https?://[^\s?]+)\?[^\s]+', r'\1?[redacted]', detail)[:1800]
            raise RuntimeError(f'Isolated Authentik acceptance failed in {phase}: {type(exc).__name__}') from None
        finally:
            if hub is not None:
                hub.terminate()
                try:hub.wait(timeout=15)
                except subprocess.TimeoutExpired:hub.kill();hub.wait(timeout=5)
            logs.close()
            docker('down','--volumes','--remove-orphans',check=False)
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(json.dumps(result,indent=2)+'\n')
            print(json.dumps(result,indent=2),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'dist/authentik-acceptance.json')
    args=parser.parse_args()
    acceptance(args.output)


if __name__=='__main__':main()
