"""OIDC routes with a deterministic signed provider; no production network/IdP."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs,urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from hub.oidc import ProviderInput,validate_claims,LOGOUT_EVENT
from hub import iam
from shared.util import DevError
from tests.test_iam_integration import team as team, Browser
from tests.test_roles import must


class Provider:
    def __init__(self):
        self.key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        self.jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwk.update(kid='key-1',use='sig',alg='RS256')
        self.issuer='https://identity.example.test/application/o/codepier/'
        self.base='https://identity.example.test'
        self.client_id='codepier-test'
        self.subject='external-alice';self.groups=['developers'];self.nonce=''
        self.overrides={};self.invalid_signature=False;self.requests=[];self.userinfo_subject=None
        self.discovery_overrides={};self.callback=None;self.expected_verifier=None
        self.on_userinfo=None;self.fail_userinfo=False;self.fail_refresh=False
        self.access_token='private-upstream-access';self.refresh_token='private-upstream-refresh'

    def sign(self,claims):
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048) if self.invalid_signature else self.key
        return jwt.encode(claims,key,algorithm='RS256',headers={'kid':'key-1'})

    def claims(self):
        now=time.time()
        return {'iss':self.issuer,'aud':self.client_id,'sub':self.subject,'iat':now,'exp':now+3600,'auth_time':now,
                'nonce':self.nonce,'groups':self.groups,'sid':'provider-session-1','name':'External Alice','email':'owner@example.test',**self.overrides}

    def handle(self,request):
        self.requests.append((request.method,str(request.url)))
        if request.url.path.endswith('/.well-known/openid-configuration'):
            body={'issuer':self.issuer,'authorization_endpoint':self.base+'/authorize','token_endpoint':self.base+'/token',
                  'jwks_uri':self.base+'/jwks','userinfo_endpoint':self.base+'/userinfo','end_session_endpoint':self.base+'/logout',
                  'response_types_supported':['code'],'id_token_signing_alg_values_supported':['RS256'],
                  'token_endpoint_auth_methods_supported':['client_secret_basic'],'code_challenge_methods_supported':['S256'],**self.discovery_overrides}
        elif request.url.path=='/jwks':body={'keys':[self.jwk]}
        elif request.url.path=='/token':
            form=parse_qs(request.content.decode())
            assert request.headers.get('authorization','').startswith('Basic ')
            if form['grant_type']==['authorization_code']:
                assert form['code']==['authorization-code']
                assert form['redirect_uri']==[self.callback]
                actual=base64.urlsafe_b64encode(hashlib.sha256(form['code_verifier'][0].encode()).digest()).rstrip(b'=').decode()
                assert actual==self.expected_verifier
            elif self.fail_refresh:return httpx.Response(400,json={'error':'invalid_grant'})
            body={'access_token':self.access_token,'refresh_token':self.refresh_token,'token_type':'Bearer','expires_in':3600,'id_token':self.sign(self.claims())}
        elif request.url.path=='/userinfo':
            assert request.headers['authorization']=='Bearer '+self.access_token
            if self.on_userinfo:self.on_userinfo()
            if self.fail_userinfo:return httpx.Response(503,json={'error':'temporary'})
            body={'sub':self.userinfo_subject or self.subject,'groups':self.groups}
        else:return httpx.Response(404,json={})
        return httpx.Response(200,json=body)


@pytest.fixture
def oidc(team):
    app,browsers=team
    fake=Provider();app.state.oidc.transport=httpx.MockTransport(fake.handle)
    config={'label':'Test SSO','issuer':fake.issuer,'client_id':fake.client_id,'client_secret':'private-client-secret',
            'admission':'jit','enabled':True,'freshness_seconds':300}
    row=must(browsers['owner'].post('/api/iam/oidc/providers',json=config),201)
    fake.callback='http://testserver/auth/oidc/'+row['id']+'/callback'
    return app,browsers,fake,row,config


def start(oidc,browser=None,*,link=False,return_to='/'):
    app,b,fake,row,_=oidc
    browser=browser or b['owner']
    if link:
        response=browser.post('/api/iam/oidc/'+row['id']+'/link',json={'return_to':return_to})
        assert response.status_code==200,response.text
        location=response.json()['redirect']
    else:
        response=browser.get('/auth/oidc/'+row['id']+'/start',params={'return_to':return_to},follow_redirects=False)
        assert response.status_code==303,response.text;location=response.headers['location']
    params=parse_qs(urlsplit(location).query)
    assert params['response_type']==['code'] and params['code_challenge_method']==['S256']
    assert 'private-client-secret' not in location
    fake.nonce=params['nonce'][0];fake.expected_verifier=params['code_challenge'][0]
    return params['state'][0],response


def callback(oidc,state,response,*,browser=None,cookie_override=None):
    app,b,fake,row,_=oidc
    browser=browser or b['owner']
    cookie=response.headers['set-cookie'].split(';',1)[0]
    return browser.get('/auth/oidc/'+row['id']+'/callback',params={'state':state,'code':'authorization-code'},
                       headers={'Cookie':cookie_override if cookie_override is not None else cookie+'; rd_session='+browser.cookie},follow_redirects=False)


def login(oidc):
    state,response=start(oidc)
    result=callback(oidc,state,response)
    assert result.status_code==303,result.text
    app,b,_,_,_=oidc
    secret=result.cookies.get('rd_session')
    session=app.state.store.one('SELECT * FROM sessions WHERE id_hash=?',(__import__('shared.crypto',fromlist=['digest']).digest(secret),))
    personal=app.state.store.one("SELECT space_id FROM memberships WHERE user_id=? AND level='owner'",(session['user_id'],))['space_id']
    return Browser(b['owner'].client,secret,session['csrf'],personal),session


def test_complete_oidc_login_provisions_nonadmin_private_space_and_encrypts_tokens(oidc):
    app,b,fake,row,_=oidc
    user,session=login(oidc)
    me=must(user.get('/api/iam/me'))
    assert me['id'] not in {'owner','alice','bob'} and not me['instance_admin'] and not me['local_login']
    assert len(me['spaces'])==1 and me['spaces'][0]['kind']=='personal'
    assert user.get('/api/iam/users').status_code==403
    assert user.get('/api/projects',headers={'X-CodePier-Space':'legacy'}).status_code==403
    identity=app.state.store.one('SELECT * FROM external_identities WHERE user_id=?',(me['id'],))
    assert identity['issuer']==fake.issuer and identity['subject']==fake.subject
    assert fake.access_token not in identity['upstream_tokens']
    assert json.loads(app.state.store.decrypt(identity['upstream_tokens']))['access_token']==fake.access_token
    assert 'private-client-secret' not in app.state.store.one('SELECT client_secret FROM oidc_providers WHERE id=?',(row['id'],))['client_secret']
    assert 'private-upstream' not in user.get('/api/audit').text
    assert session['user_id']==me['id']


@pytest.mark.parametrize('claim,value',[
    ('iss','https://other-issuer.test/'),('aud','another-client'),('azp','other-client'),
    ('exp',1),('iat',9999999999),('nonce','wrong-nonce'),('sub',''),('sub','非ASCII'),
    ('aud',['codepier-test','other']),('at_hash','wrong'),('sid',{'bad':'shape'}),('exp',True),
])
def test_invalid_signed_claims_rejected_without_provisioning(oidc,claim,value):
    app,b,fake,_,_=oidc;fake.overrides[claim]=value
    state,response=start(oidc);out=callback(oidc,state,response)
    assert out.status_code==401,out.text
    assert app.state.store.one('SELECT count(*) AS n FROM external_identities')['n']==0
    assert fake.access_token not in out.text


def test_wrong_signature_and_userinfo_subject_are_rejected(oidc):
    app,b,fake,_,_=oidc
    fake.invalid_signature=True
    state,response=start(oidc);assert callback(oidc,state,response).status_code==401
    fake.invalid_signature=False;fake.userinfo_subject='another-subject'
    state,response=start(oidc);assert callback(oidc,state,response).status_code==401
    assert app.state.store.one('SELECT count(*) AS n FROM external_identities')['n']==0


def test_state_is_single_use_and_bound_to_browser(oidc):
    app,b,_,_,_=oidc
    state,response=start(oidc)
    assert callback(oidc,state,response,cookie_override='rd_session='+b['owner'].cookie).status_code==400
    accepted=callback(oidc,state,response);assert accepted.status_code==303,accepted.text
    assert callback(oidc,state,response).status_code==400


def test_oidc_login_preserves_downstream_oauth_continuation(oidc):
    app,b,_,_,_=oidc
    state,response=start(oidc,return_to='/?authorize=expected-request#connect')
    result=callback(oidc,state,response)
    assert result.status_code==303 and result.headers['location']=='/?authorize=expected-request#connect'


@pytest.mark.parametrize('return_to',['https://evil.test/','//evil.test/','/api/login','/?other=secret','/\\evil.test/'])
def test_arbitrary_return_redirect_is_rejected(oidc,return_to):
    app,b,_,row,_=oidc
    response=b['owner'].get('/auth/oidc/'+row['id']+'/start',params={'return_to':return_to},follow_redirects=False)
    assert response.status_code==400


def test_closed_admission_does_not_admit_unknown_subject(oidc):
    app,b,fake,row,config=oidc
    must(b['owner'].put('/api/iam/oidc/providers/'+row['id'],json={**config,'admission':'closed','expected_version':row['version']}))
    state,response=start(oidc);assert callback(oidc,state,response).status_code==403
    assert app.state.store.one('SELECT count(*) AS n FROM external_identities')['n']==0


def test_explicit_link_does_not_link_by_email_and_requires_original_session(oidc):
    app,b,fake,row,_=oidc
    state,response=start(oidc,b['alice'],link=True)
    assert parse_qs(urlsplit(response.json()['redirect']).query)['prompt']==['login']
    wrong=callback(oidc,state,response,browser=b['bob']);assert wrong.status_code==401
    accepted=callback(oidc,state,response,browser=b['alice']);assert accepted.status_code==303,accepted.text
    identity=app.state.store.one('SELECT * FROM external_identities')
    assert identity['user_id']=='alice'
    assert identity['user_id']!='owner'


def test_link_rejects_stale_upstream_authentication(oidc):
    app,b,fake,_,_=oidc;fake.overrides['auth_time']=1
    state,response=start(oidc,b['alice'],link=True)
    assert callback(oidc,state,response,browser=b['alice']).status_code==401


def test_group_sync_removes_only_derived_memberships_and_assignments(oidc):
    app,b,fake,row,_=oidc
    r=must(b['owner'].post('/api/access-roles',json={'label':'shared','project_rules':[{'actions':['read'],'all_projects':True}], 'idempotency_key':'group-role-create'}),201)
    must(b['owner'].post('/api/iam/oidc/providers/'+row['id']+'/mappings',json={'group_name':'developers','space_id':'team','role_id':r['id'],'may_delegate':True}),201)
    user,session=login(oidc);uid=session['user_id']
    assert any(s['id']=='team' for s in must(user.get('/api/iam/me'))['spaces'])
    # Explicit manual membership and Role assignment must not be removed by group sync.
    store=app.state.store
    store.execute("INSERT INTO memberships(space_id,user_id,source,level) VALUES('team',?,'manual','member')",(uid,))
    store.execute("INSERT INTO role_assignments(role_id,user_id,space_id,source,may_delegate) VALUES(?,?,'team','manual',1)",(r['id'],uid))
    identity=store.one('SELECT * FROM external_identities WHERE user_id=?',(uid,))
    fake.groups=[]
    asyncio.run(app.state.oidc.sync_identity(identity['id']))
    assert store.one("SELECT active FROM memberships WHERE user_id=? AND source='manual' AND space_id='team'",(uid,))['active']==1
    assert all(not x['active'] for x in store.all("SELECT active FROM memberships WHERE user_id=? AND source LIKE 'oidc:%'",(uid,)))
    assert all(not x['active'] for x in store.all("SELECT active FROM role_assignments WHERE user_id=? AND source LIKE 'oidc:%'",(uid,)))
    assert store.one("SELECT active FROM role_assignments WHERE user_id=? AND source='manual'",(uid,))['active']==1


def test_freshness_expiry_denies_resources_but_not_logout(oidc):
    app,b,fake,_,_=oidc;user,session=login(oidc)
    identity=app.state.store.one('SELECT * FROM external_identities WHERE user_id=?',(session['user_id'],))
    app.state.store.execute('UPDATE external_identities SET fresh_until=0 WHERE id=?',(identity['id'],))
    fake.fail_userinfo=True
    with pytest.raises(DevError):asyncio.run(app.state.oidc.sync_identity(identity['id']))
    assert app.state.store.one('SELECT fresh_until FROM external_identities WHERE id=?',(identity['id'],))['fresh_until']==0
    assert user.get('/api/projects').status_code==403
    assert user.get('/api/iam/me').status_code==200
    assert user.post('/api/logout').status_code==200


def test_unlink_tombstone_and_sync_race_do_not_recreate_access(oidc):
    app,b,fake,row,_=oidc
    state,response=start(oidc,b['alice'],link=True)
    result=callback(oidc,state,response,browser=b['alice']);assert result.status_code==303
    store=app.state.store;identity=store.one("SELECT * FROM external_identities WHERE user_id='alice'")
    def unlink_during_request():
        with store.transaction():app.state.oidc.disable_identity(identity,'unlinked',revoke=True)
    fake.on_userinfo=unlink_during_request
    asyncio.run(app.state.oidc.sync_identity(identity['id']))
    current=store.one('SELECT * FROM external_identities WHERE id=?',(identity['id'],))
    assert not current['enabled'] and current['disabled_reason']=='unlinked' and current['upstream_tokens'] is None
    fake.on_userinfo=None
    state,response=start(oidc,b['bob']);assert callback(oidc,state,response,browser=b['bob']).status_code==403
    assert store.one('SELECT user_id FROM external_identities WHERE id=?',(identity['id'],))['user_id']=='alice'


def test_configuration_changes_invalidate_active_entitlements(oidc):
    app,b,fake,row,config=oidc
    state,response=start(oidc)
    must(b['owner'].put('/api/iam/oidc/providers/'+row['id'],json={**config,'expected_version':row['version'],'required_group':'new-group'}))
    assert callback(oidc,state,response).status_code==409
    assert app.state.store.one('SELECT count(*) AS n FROM external_identities')['n']==0


def test_backchannel_logout_is_signed_scoped_and_replay_protected(oidc):
    app,b,fake,row,_=oidc;user,session=login(oidc)
    now=time.time()
    claims={'iss':fake.issuer,'aud':fake.client_id,'iat':now,'jti':'one-logout','sid':'provider-session-1','events':{LOGOUT_EVENT:{}}}
    signed=fake.sign(claims)
    response=b['bob'].post('/auth/oidc/'+row['id']+'/backchannel-logout',data={'logout_token':signed})
    assert response.status_code==200,response.text
    assert user.get('/api/iam/me').status_code==401
    assert b['bob'].get('/api/iam/me').status_code==200
    assert b['bob'].post('/auth/oidc/'+row['id']+'/backchannel-logout',data={'logout_token':signed}).status_code==400


@pytest.mark.parametrize('override',[
    {'issuer':'https://wrong-issuer.test/'},
    {'jwks_uri':'https://unapproved-origin.test/jwks'},
    {'token_endpoint':'http://identity.example.test/token'},
    {'code_challenge_methods_supported':['plain']},
    {'id_token_signing_alg_values_supported':['HS256']},
])
def test_discovery_does_not_trust_wrong_issuer_endpoints_or_algorithms(oidc,override):
    app,b,fake,row,_=oidc;fake.discovery_overrides=override
    response=b['owner'].get('/auth/oidc/'+row['id']+'/start',follow_redirects=False)
    assert response.status_code==400,response.text


def test_provider_configuration_is_instance_only_and_no_tokens_in_metadata(oidc):
    app,b,fake,row,config=oidc
    assert b['alice'].get('/api/iam/oidc/providers').status_code==403
    assert b['alice'].post('/api/iam/oidc/providers',json=config).status_code==403
    body=must(b['owner'].get('/api/iam/oidc/providers'))
    assert 'private-client-secret' not in json.dumps(body)
    assert body['providers'][0]['has_client_secret']
    assert b['owner'].put('/api/iam/oidc/providers/'+row['id'],json={**config,'issuer':'https://another.test/','expected_version':row['version']}).status_code==409


@pytest.mark.parametrize('issuer',['http://identity.test/','https://user:password@identity.test/','https://identity.test/#fragment','https://identity.test/?query=1'])
def test_invalid_provider_urls_are_rejected(issuer):
    with pytest.raises(ValueError):ProviderInput(label='invalid',issuer=issuer,client_id='client',client_secret='secret')
