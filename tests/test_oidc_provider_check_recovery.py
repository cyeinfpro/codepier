"""Disabled-provider diagnostics must not require enabling external login."""
from __future__ import annotations

import httpx
import pytest

from tests.test_iam_integration import team as team
from tests.test_oidc_integration import Provider
from tests.test_roles import must


@pytest.fixture
def disabled_provider(team):
    app, browsers = team
    fake = Provider()
    app.state.oidc.transport = httpx.MockTransport(fake.handle)
    config = {
        'label': 'Unpublished SSO', 'issuer': fake.issuer,
        'client_id': fake.client_id, 'client_secret': 'synthetic-client-secret',
        'admission': 'closed',
    }
    row = must(browsers['owner'].post('/api/iam/oidc/providers', json=config), 201)
    assert row['enabled'] == 0
    return app, browsers, fake, row


def test_disabled_provider_discovery_check_does_not_enable_login(disabled_provider):
    app, browsers, fake, row = disabled_provider
    url = '/api/iam/oidc/providers/' + row['id'] + '/check'
    for _ in range(2):
        response = browsers['owner'].post(url)
        assert response.status_code == 200, response.text
        assert response.json()['issuer'] == fake.issuer
        assert response.json()['signing_keys'] == 1
    assert len(fake.requests) == 4
    current = app.state.store.one('SELECT enabled,version FROM oidc_providers WHERE id=?', (row['id'],))
    assert current == {'enabled': 0, 'version': row['version']}
    assert browsers['owner'].get('/api/auth/providers').json() == {'providers': []}
    assert browsers['owner'].get('/auth/oidc/' + row['id'] + '/start',
                                  follow_redirects=False).status_code == 404
    assert len(fake.requests) == 4


def test_disabled_provider_check_requires_instance_admin_and_csrf(disabled_provider):
    _, browsers, fake, row = disabled_provider
    url = '/api/iam/oidc/providers/' + row['id'] + '/check'
    assert browsers['bob'].post(url).status_code == 403
    assert browsers['owner'].post(url, headers={'X-RD-CSRF': 'invalid'}).status_code == 403
    assert fake.requests == []


def test_disabled_provider_check_rejects_concurrent_config_change(disabled_provider):
    app, browsers, fake, row = disabled_provider
    def changed(request):
        response = fake.handle(request)
        if request.url.path == '/jwks':
            app.state.store.execute('UPDATE oidc_providers SET version=version+1 WHERE id=?', (row['id'],))
        return response
    app.state.oidc.transport = httpx.MockTransport(changed)
    response = browsers['owner'].post('/api/iam/oidc/providers/' + row['id'] + '/check')
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'OIDC_CONFIG_CHANGED'
    assert app.state.store.one('SELECT * FROM oidc_cache WHERE provider_id=?', (row['id'],)) is None
    assert app.state.store.one('SELECT enabled FROM oidc_providers WHERE id=?', (row['id'],))['enabled'] == 0
