"""Environment-seeded OIDC provider and first-login instance administrator on a fresh Hub."""
from __future__ import annotations
import sys
from urllib.parse import parse_qs,urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from hub import __main__ as cli
from hub.app import create_app
from hub.config import OIDC_SEED_PROVIDER_ID,HubConfig
from shared.config import ConfigurationError
from shared.crypto import digest
from tests.test_oidc_integration import Provider

SEED={'CODEPIER_OIDC_LABEL':'Company SSO','CODEPIER_OIDC_CLIENT_ID':'codepier-test','CODEPIER_OIDC_CLIENT_SECRET':'private-client-secret',
      'CODEPIER_OIDC_SCOPES':'openid profile email','CODEPIER_OIDC_FRESHNESS_SECONDS':'300','CODEPIER_OIDC_BOOTSTRAP_ADMIN':'first-login'}


def seed_env(monkeypatch,fake,**overrides):
    monkeypatch.setenv('HUB_PUBLIC_URL','http://testserver');monkeypatch.setenv('MCP_PUBLIC_URL','')
    for key,value in {**SEED,'CODEPIER_OIDC_ISSUER':fake.issuer,**overrides}.items():
        if value is None:monkeypatch.delenv(key,raising=False)
        else:monkeypatch.setenv(key,value)


def fresh_app(directory,fake):
    app=create_app(str(directory))
    app.state.oidc.transport=httpx.MockTransport(fake.handle)
    fake.callback='http://testserver/auth/oidc/'+OIDC_SEED_PROVIDER_ID+'/callback'
    return app


def sso_login(client,fake,subject):
    """Anonymous browser: no prior session, only the login transaction cookie."""
    client.cookies.clear();fake.subject=subject
    response=client.get('/auth/oidc/'+OIDC_SEED_PROVIDER_ID+'/start',params={'return_to':'/'},follow_redirects=False)
    assert response.status_code==303,response.text
    params=parse_qs(urlsplit(response.headers['location']).query)
    fake.nonce=params['nonce'][0];fake.expected_verifier=params['code_challenge'][0]
    cookie=response.headers['set-cookie'].split(';',1)[0]
    result=client.get('/auth/oidc/'+OIDC_SEED_PROVIDER_ID+'/callback',params={'state':params['state'][0],'code':'authorization-code'},
                      headers={'Cookie':cookie},follow_redirects=False)
    client.cookies.clear()
    return result


def session_of(client,result):
    assert result.status_code==303,result.text
    secret=result.cookies.get('rd_session')
    me=client.get('/api/iam/me',headers={'Cookie':'rd_session='+secret})
    assert me.status_code==200,me.text
    return me.json(),secret


def test_first_sso_login_becomes_instance_admin_then_the_window_closes(tmp_path,monkeypatch):
    fake=Provider();seed_env(monkeypatch,fake)
    app=fresh_app(tmp_path/'hub',fake)
    with TestClient(app,raise_server_exceptions=True) as client:
        store=app.state.store
        assert client.get('/api/auth/providers').json()['providers']==[{'id':OIDC_SEED_PROVIDER_ID,'label':'Company SSO'}]
        assert not store.one('SELECT 1 AS ok FROM iam_users WHERE instance_admin=1')
        admin,admin_secret=session_of(client,sso_login(client,fake,'external-alice'))
        assert admin['instance_admin'] and not admin['local_login']
        levels={s['id']:s['level'] for s in admin['spaces']}
        assert levels['legacy']=='owner' and any(s['kind']=='personal' for s in admin['spaces'])
        assert client.get('/api/iam/users',headers={'Cookie':'rd_session='+admin_secret}).status_code==200
        second,second_secret=session_of(client,sso_login(client,fake,'external-bob'))
        assert not second['instance_admin'] and 'legacy' not in {s['id'] for s in second['spaces']}
        assert client.get('/api/iam/users',headers={'Cookie':'rd_session='+second_secret}).status_code==403
        actions=[r['action'] for r in store.all("SELECT action FROM audit WHERE action LIKE 'oidc.%' ORDER BY at,rowid")]
        assert actions.count('oidc.bootstrap_admin')==1 and actions.count('oidc.login')==2
        assert store.one('SELECT count(*) AS n FROM iam_users WHERE instance_admin=1')['n']==1


def test_bootstrap_stays_closed_without_explicit_opt_in(tmp_path,monkeypatch):
    fake=Provider();seed_env(monkeypatch,fake,CODEPIER_OIDC_BOOTSTRAP_ADMIN=None)
    app=fresh_app(tmp_path/'hub',fake)
    with TestClient(app,raise_server_exceptions=True) as client:
        result=sso_login(client,fake,'external-alice')
        assert result.status_code==403 and result.json()['error']['code']=='BOOTSTRAP_REQUIRED'
        assert not app.state.store.one('SELECT 1 AS ok FROM users')


def test_required_group_bounds_who_may_bootstrap(tmp_path,monkeypatch):
    fake=Provider();seed_env(monkeypatch,fake,CODEPIER_OIDC_REQUIRED_GROUP='codepier-admins')
    app=fresh_app(tmp_path/'hub',fake)
    with TestClient(app,raise_server_exceptions=True) as client:
        denied=sso_login(client,fake,'outsider')
        assert denied.status_code==403 and not app.state.store.one('SELECT 1 AS ok FROM users')
        fake.groups=['codepier-admins']
        admin,_=session_of(client,sso_login(client,fake,'external-alice'))
        assert admin['instance_admin']


def test_environment_stays_authoritative_for_the_seeded_provider(tmp_path,monkeypatch):
    fake=Provider();seed_env(monkeypatch,fake)
    directory=tmp_path/'hub'
    app=fresh_app(directory,fake)
    with TestClient(app,raise_server_exceptions=True) as client:
        admin,secret=session_of(client,sso_login(client,fake,'external-alice'))
        headers={'Cookie':'rd_session='+secret,'X-RD-CSRF':app.state.store.one('SELECT csrf FROM sessions WHERE id_hash=?',(digest(secret),))['csrf']}
        row=client.get('/api/iam/oidc/providers',headers=headers).json()['providers'][0]
        assert row['id']==OIDC_SEED_PROVIDER_ID and row['version']==1 and row['admission']=='jit' and row['has_client_secret']
        edit=client.put('/api/iam/oidc/providers/'+OIDC_SEED_PROVIDER_ID,json={'label':'Renamed in panel','issuer':fake.issuer,'client_id':fake.client_id,'expected_version':1},headers=headers)
        assert edit.status_code==409 and edit.json()['error']['code']=='PROVIDER_ENV_MANAGED'
        created=client.post('/api/iam/oidc/providers',json={'label':'Second','issuer':'https://other.example.test/','client_id':'x','client_secret':'y'},headers=headers)
        assert created.status_code==201
    # An unchanged environment does not rewrite the provider or invalidate entitlements.
    with TestClient(fresh_app(directory,fake),raise_server_exceptions=True) as client:
        assert client.app.state.store.one('SELECT version FROM oidc_providers WHERE id=?',(OIDC_SEED_PROVIDER_ID,))['version']==1
    # A changed setting is applied on the next start; the identity keeps working.
    monkeypatch.setenv('CODEPIER_OIDC_LABEL','Company SSO v2')
    with TestClient(fresh_app(directory,fake),raise_server_exceptions=True) as client:
        row=client.app.state.store.one('SELECT label,version FROM oidc_providers WHERE id=?',(OIDC_SEED_PROVIDER_ID,))
        assert (row['label'],row['version'])==('Company SSO v2',2)
        again,_=session_of(client,sso_login(client,fake,'external-alice'))
        assert again['instance_admin'] and again['id']==admin['id']
        assert [r['actor'] for r in client.app.state.store.all("SELECT actor FROM audit WHERE action='oidc.provider_updated'")]==['local-env']
    # Rebinding linked identities to another issuer is refused at startup.
    monkeypatch.setenv('CODEPIER_OIDC_ISSUER','https://identity.example.test/application/o/other/')
    with pytest.raises(ConfigurationError,match='CODEPIER_OIDC_ISSUER'):
        with TestClient(fresh_app(directory,fake),raise_server_exceptions=True):pass
    # Without linked identities the issuer may change.
    with TestClient(fresh_app(tmp_path/'unbound',fake),raise_server_exceptions=True):pass
    monkeypatch.setenv('CODEPIER_OIDC_ISSUER',fake.issuer)
    with TestClient(fresh_app(tmp_path/'unbound',fake),raise_server_exceptions=True) as client:
        assert client.app.state.store.one('SELECT issuer FROM oidc_providers WHERE id=?',(OIDC_SEED_PROVIDER_ID,))['issuer']==fake.issuer
    # Removing the variables keeps the provider and hands it back to the panel.
    seed_env(monkeypatch,fake,CODEPIER_OIDC_ISSUER=None)
    with TestClient(fresh_app(directory,fake),raise_server_exceptions=True) as client:
        admin,secret=session_of(client,sso_login(client,fake,'external-alice'))
        headers={'Cookie':'rd_session='+secret,'X-RD-CSRF':client.app.state.store.one('SELECT csrf FROM sessions WHERE id_hash=?',(digest(secret),))['csrf']}
        row=client.get('/api/iam/oidc/providers',headers=headers).json()['providers'][0]
        edit=client.put('/api/iam/oidc/providers/'+OIDC_SEED_PROVIDER_ID,json={'label':'Panel managed','issuer':fake.issuer,'client_id':fake.client_id,'enabled':False,'expected_version':row['version']},headers=headers)
        assert edit.status_code==200 and edit.json()['label']=='Panel managed' and not edit.json()['enabled']


@pytest.mark.parametrize('key,value,setting',[
    ('CODEPIER_OIDC_SCOPES','profile email','CODEPIER_OIDC_SCOPES'),
    ('CODEPIER_OIDC_ISSUER','http://insecure.example.test/','CODEPIER_OIDC_ISSUER'),
    ('CODEPIER_OIDC_DISCOVERY_URL','https://elsewhere.example.test/.well-known/openid-configuration','CODEPIER_OIDC_DISCOVERY_URL'),
    ('CODEPIER_OIDC_DISCOVERY_URL','http://identity.example.test/insecure','CODEPIER_OIDC_DISCOVERY_URL'),
    ('CODEPIER_OIDC_ENDPOINT_ORIGINS','https://cdn.example.test/path','CODEPIER_OIDC_ENDPOINT_ORIGINS'),
    ('CODEPIER_OIDC_CLIENT_SECRET','','CODEPIER_OIDC_CLIENT_SECRET'),
    ('CODEPIER_OIDC_FRESHNESS_SECONDS','10','CODEPIER_OIDC_FRESHNESS_SECONDS'),
    ('CODEPIER_OIDC_ENABLED','maybe','CODEPIER_OIDC_ENABLED'),
    ('CODEPIER_OIDC_BOOTSTRAP_ADMIN','yes','CODEPIER_OIDC_BOOTSTRAP_ADMIN'),
])
def test_invalid_seed_settings_fail_before_startup_naming_only_the_setting(monkeypatch,key,value,setting):
    fake=Provider();seed_env(monkeypatch,fake,**{key:value})
    with pytest.raises(ConfigurationError) as info:
        HubConfig.from_env()
    text=str(info.value)
    assert setting in text and 'private-client-secret' not in text
    assert not value or value not in text.replace(setting,'')


def test_no_seed_means_no_provider_and_no_bootstrap(tmp_path,monkeypatch):
    fake=Provider();seed_env(monkeypatch,fake,CODEPIER_OIDC_ISSUER=None)
    assert HubConfig.from_env().oidc_seed is None
    app=fresh_app(tmp_path/'hub',fake)
    with TestClient(app,raise_server_exceptions=True) as client:
        assert client.get('/api/auth/providers').json()['providers']==[]
        assert not app.state.store.one('SELECT 1 AS ok FROM oidc_providers')


def test_local_recovery_administrator_can_be_added_after_sso_bootstrap(tmp_path,monkeypatch,capsys):
    fake=Provider();seed_env(monkeypatch,fake)
    app=fresh_app(tmp_path/'hub',fake)
    with TestClient(app,raise_server_exceptions=True) as client:
        session_of(client,sso_login(client,fake,'external-alice'))
        monkeypatch.setenv('CODEPIER_ADMIN_PASSWORD','recovery-password-123')
        monkeypatch.setattr(sys,'argv',['hub','--data-dir',str(tmp_path/'hub'),'init','--username','recovery'])
        cli.main()
        row=app.state.store.one("SELECT s.instance_admin,s.local_login,m.level FROM users u JOIN iam_users s ON s.user_id=u.id JOIN memberships m ON m.user_id=u.id AND m.space_id='legacy' WHERE u.username='recovery'")
        assert row=={'instance_admin':1,'local_login':1,'level':'owner'}
        client.cookies.clear()
        login=client.post('/api/login',json={'username':'recovery','password':'recovery-password-123'})
        assert login.status_code==200,login.text
        assert client.get('/api/iam/users').status_code==200
        # A second init is refused once a local login exists; the SSO administrator is untouched.
        with pytest.raises(SystemExit):
            cli.main()
        assert app.state.store.one('SELECT count(*) AS n FROM iam_users WHERE instance_admin=1 AND active=1')['n']==2
