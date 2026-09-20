"""Regression for the HTTPS Origin failure after the Compose project rename."""
import copy
import json
import os
from pathlib import Path
import stat

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from hub.auth import Auth
from scripts import migrate_hub_proxy as proxy
from shared.util import DevError

OLD = '172.20.0.1'
NEW = '172.21.0.1'


class Docker:
    def __init__(self, root):
        self.root = root
        self.fixed = None
        self.config = {'services': {'hub': {'environment': {proxy.KEY: '127.0.0.1,' + OLD},
                                          'networks': {'default': {}}}},
                       'networks': {'default': {'name': 'codepier_default'}}}
        self.old = {'Config': {'Labels': {'com.docker.compose.service': 'hub'}},
                    'NetworkSettings': {'Networks': {'remote-dev-mcp_default': {'Gateway': OLD}}}}
        self.networks = {name: {'Driver': 'bridge', 'Labels': {
            'com.docker.compose.project': project, 'com.docker.compose.network': 'default'},
            'IPAM': {'Config': [{'Gateway': gateway}]}}
            for name, project, gateway in [('remote-dev-mcp_default', 'remote-dev-mcp', OLD),
                                            ('codepier_default', 'codepier', NEW)]}

    def json(self, args):
        if args[0] == 'network':
            return [copy.deepcopy(self.networks[args[2]])]
        config = copy.deepcopy(self.config)
        if self.fixed is not None:
            config['services']['hub']['environment'][proxy.KEY] = self.fixed
        else:
            # The production path asks Docker Compose to resolve its own .env.
            import shlex
            for line in (self.root / '.env').read_text().splitlines():
                if line.startswith(proxy.KEY + '='):
                    config['services']['hub']['environment'][proxy.KEY] = shlex.split(line.split('=', 1)[1], comments=True)[0]
        return config


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv(proxy.KEY, raising=False)
    (tmp_path / '.env').write_text('# retained\nOTHER=keep\nFORWARDED_ALLOW_IPS=127.0.0.1,' + OLD + '\n')
    (tmp_path / '.env').chmod(0o600)
    docker = Docker(tmp_path)
    plan = proxy.preflight(docker.config, [docker.old], docker, 'remote-dev-mcp')
    return tmp_path, docker, plan


def origin_request(trusted, client_ip=NEW, origin='https://hub.example.com'):
    async def endpoint(request: Request):
        Auth.check_origin(request)
        return JSONResponse({'ok': True})
    app = ProxyHeadersMiddleware(Starlette(routes=[Route('/', endpoint, methods=['POST'])]), trusted_hosts=trusted)
    with TestClient(app, client=(client_ip, 1234)) as client:
        return client.post('/', headers={'Host': 'hub.example.com', 'Origin': origin,
                                        'X-Forwarded-Proto': 'https', 'Sec-Fetch-Site': 'same-origin'})


def test_upgrade_restores_https_origin_without_relaxing_protection(setup):
    root, docker, plan = setup
    with pytest.raises(DevError, match='不接受跨站'):
        origin_request('127.0.0.1,' + OLD)
    result = proxy.apply(root, plan, docker)
    assert result['current'] == '127.0.0.1,' + NEW
    assert origin_request(result['current']).status_code == 200
    with pytest.raises(DevError, match='不接受跨站'):
        origin_request(result['current'], origin='https://evil.invalid')
    with pytest.raises(DevError, match='不接受跨站'):
        origin_request(result['current'], client_ip='198.51.100.8')
    assert '# retained\nOTHER=keep\n' in (root / '.env').read_text()
    assert stat.S_IMODE((root / '.env').stat().st_mode) == 0o600
    assert proxy.apply(root, plan, docker) == {'changed': False}


def test_resume_uses_saved_plan_after_legacy_network_is_retired(setup):
    root, docker, plan = setup
    saved = json.loads(json.dumps(plan))
    del docker.networks['remote-dev-mcp_default']
    assert proxy.apply(root, saved, docker)['changed']


@pytest.mark.parametrize('trusted', ['127.0.0.1', '172.30.87.2', '10.0.0.8', '*', '172.20.0.0/16'])
def test_no_new_trust_is_granted_to_unlisted_gateways(setup, trusted):
    root, docker, _ = setup
    docker.config['services']['hub']['environment'][proxy.KEY] = trusted
    assert proxy.preflight(docker.config, [docker.old], docker, 'remote-dev-mcp') == []


def test_retains_other_explicit_proxies_and_handles_quoted_env(setup):
    root, docker, plan = setup
    (root / '.env').write_text('FORWARDED_ALLOW_IPS="127.0.0.1,' + OLD + ',10.0.0.8" # proxy\n')
    assert proxy.apply(root, plan, docker)['current'] == '127.0.0.1,' + NEW + ',10.0.0.8'


@pytest.mark.parametrize('problem', ['foreign', 'external', 'missing-gateway', 'override', 'export', 'duplicate', 'symlink'])
def test_ambiguous_or_overridden_settings_preserve_env(setup, monkeypatch, problem):
    root, docker, plan = setup
    path = root / '.env'
    if problem == 'foreign':
        docker.networks['codepier_default']['Labels']['com.docker.compose.project'] = 'other'
    elif problem == 'external':
        docker.config['networks']['default']['external'] = True
    elif problem == 'missing-gateway':
        docker.networks['codepier_default']['IPAM']['Config'] = []
    elif problem == 'override':
        docker.fixed = '127.0.0.1,' + OLD
    elif problem == 'export':
        monkeypatch.setenv(proxy.KEY, '127.0.0.1,' + OLD)
    elif problem == 'duplicate':
        path.write_text(path.read_text() + 'FORWARDED_ALLOW_IPS=127.0.0.1,' + OLD + '\n')
    elif problem == 'symlink':
        path.rename(root / 'private-env');path.symlink_to(root / 'private-env')
    before = path.read_bytes()
    with pytest.raises(RuntimeError):
        proxy.apply(root, plan, docker)
    assert path.read_bytes() == before


def test_foreign_legacy_network_is_not_adopted(setup):
    _, docker, _ = setup
    docker.networks['remote-dev-mcp_default']['Labels']['com.docker.compose.project'] = 'other'
    with pytest.raises(RuntimeError, match='unmanaged'):
        proxy.preflight(docker.config, [docker.old], docker, 'remote-dev-mcp')


def test_installer_updates_trust_after_network_creation_before_public_start():
    script = (Path(__file__).resolve().parents[1] / 'deploy/install-hub.sh').read_text()
    assert script.index('docker compose run --rm --no-deps hub') < script.index('migrate_hub.py proxy-trust') < script.index('docker compose up -d')
