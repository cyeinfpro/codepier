"""Review regression cases against persistent scheduling and actual OIDC routes."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from hub import iam
from hub.oidc import LOGOUT_EVENT, SigningKeyRefreshRequired
from hub.oidc_resilience import SyncScheduler
from shared.util import DevError
from tests.test_oidc_integration import oidc as oidc, team as team, start, callback, login
from tests.test_roles import must


def seed_identity(app, provider, identifier='sync-user', **overrides):
    store=app.state.store
    row=dict(id=identifier,issuer=provider['issuer'],subject=identifier,provider_id=provider['id'],
             user_id='alice',checked_at=time.time()-600,fresh_until=time.time()+300,
             upstream_tokens=store.encrypt(json.dumps({'access_token':'private-upstream-access',
                 'refresh_token':'private-upstream-refresh','expires_at':time.time()+3600})),created=time.time())
    row.update(overrides)
    store.execute('INSERT INTO external_identities ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
    return store.one('SELECT * FROM external_identities WHERE id=?',(identifier,))


def run_sync(app):
    asyncio.run(app.state.oidc.reconcile())


@pytest.mark.parametrize('mode',['missing_userinfo','expired_without_refresh','outage'])
def test_unsuccessful_sync_is_scheduled_without_renewing_freshness(oidc,mode):
    app,b,fake,provider,_=oidc
    row=seed_identity(app,provider,subject=fake.subject)
    if mode=='missing_userinfo':fake.discovery_overrides['userinfo_endpoint']=None
    if mode=='expired_without_refresh':
        app.state.store.execute('UPDATE external_identities SET upstream_tokens=? WHERE id=?',
            (app.state.store.encrypt(json.dumps({'access_token':fake.access_token,'expires_at':0})),row['id']))
    if mode=='outage':fake.fail_userinfo=True
    run_sync(app)
    state=app.state.store.one('SELECT * FROM oidc_sync_state WHERE identity_id=?',(row['id'],))
    current=app.state.store.one('SELECT * FROM external_identities WHERE id=?',(row['id'],))
    assert state['failures']==1 and state['next_attempt']>time.time()
    assert state['attempted_at']>row['checked_at']
    assert current['fresh_until']==row['fresh_until'] and current['checked_at']==row['checked_at']
    requests=len(fake.requests)
    # A new scheduler instance reuses durable backoff (as after process restart).
    app.state.oidc.scheduler=SyncScheduler(app.state.oidc)
    run_sync(app)
    assert len(fake.requests)==requests
    assert app.state.store.one('SELECT failures FROM oidc_sync_state')['failures']==1
    app.state.store.execute('UPDATE oidc_sync_state SET next_attempt=0')
    run_sync(app)
    again=app.state.store.one('SELECT * FROM oidc_sync_state')
    assert again['failures']==2 and 110<again['next_attempt']-time.time()<=120
    assert app.state.store.one('SELECT fresh_until FROM external_identities')['fresh_until']==row['fresh_until']


def test_failing_provider_cannot_monopolize_oldest_batch(oidc,monkeypatch):
    app,b,fake,provider,config=oidc
    other=must(b['owner'].post('/api/iam/oidc/providers',json={**config,'issuer':'https://healthy.example.test/'}),201)
    for n in range(40):seed_identity(app,provider,f'broken-{n:03}')
    healthy=seed_identity(app,other,'healthy',checked_at=time.time()-61)
    visited=[]
    async def synchronize(identifier):
        visited.append(identifier)
        if identifier.startswith('broken'):return 'OIDC_USERINFO_UNAVAILABLE'
        app.state.store.execute('UPDATE external_identities SET checked_at=?,fresh_until=? WHERE id=?',(time.time(),time.time()+300,identifier))
    monkeypatch.setattr(app.state.oidc,'sync_identity',synchronize)
    run_sync(app)
    assert 'healthy' in visited[:4] and len(visited)==32
    run_sync(app)
    assert len(set(visited))==41
    assert app.state.store.one('SELECT fresh_until FROM external_identities WHERE id=?',('healthy',))['fresh_until']>=healthy['fresh_until']
    events=app.state.store.all("SELECT * FROM audit WHERE actor='oidc-worker'")
    assert len(events)==1  # 40 failed identities, one provider summary.
    assert app.state.store.one('SELECT suppressed FROM oidc_sync_audit')['suppressed']==39


def test_outage_audit_coalescing_is_persistent_and_bounded(oidc,monkeypatch):
    app,_,_,provider,_=oidc
    for n in range(32):seed_identity(app,provider,f'outage-{n}')
    original=app.state.store.all('SELECT id,fresh_until FROM external_identities ORDER BY id')
    async def unavailable(identifier):raise DevError('OIDC_UPSTREAM_ERROR','provider unavailable',502)
    monkeypatch.setattr(app.state.oidc,'sync_identity',unavailable)
    run_sync(app)
    for _ in range(4):
        app.state.store.execute('UPDATE oidc_sync_state SET next_attempt=0')
        run_sync(app)
    assert len(app.state.store.all("SELECT id FROM audit WHERE actor='oidc-worker'"))==1
    state=app.state.store.one('SELECT * FROM oidc_sync_audit')
    assert state['suppressed']==159
    app.state.store.execute('UPDATE oidc_sync_audit SET emitted_at=emitted_at-3601')
    app.state.store.execute('UPDATE oidc_sync_state SET next_attempt=0')
    app.state.oidc.scheduler=SyncScheduler(app.state.oidc)
    run_sync(app)
    events=app.state.store.all("SELECT detail FROM audit WHERE actor='oidc-worker' ORDER BY id")
    assert len(events)==2 and json.loads(events[-1]['detail'])['suppressed']==159
    assert app.state.store.all('SELECT id,fresh_until FROM external_identities ORDER BY id')==original


def test_new_login_invalidates_old_backoff_without_stale_completion(oidc,monkeypatch):
    app,_,_,provider,_=oidc;row=seed_identity(app,provider)
    async def newer_login(identifier):
        app.state.store.execute('UPDATE external_identities SET upstream_tokens=?,checked_at=?,fresh_until=? WHERE id=?',
            (app.state.store.encrypt('{}'),time.time(),time.time()+800,identifier))
        raise DevError('OIDC_CREDENTIAL_REJECTED','old credentials',401)
    monkeypatch.setattr(app.state.oidc,'sync_identity',newer_login)
    run_sync(app)
    current=app.state.store.one('SELECT * FROM external_identities')
    assert current['enabled']==1 and current['fresh_until']>row['fresh_until']
    assert app.state.store.one('SELECT failures FROM oidc_sync_state')['failures']==0
    assert not app.state.store.all("SELECT id FROM audit WHERE actor='oidc-worker'")


def group_policy(oidc,kind):
    app,b,fake,provider,config=oidc
    if kind=='required':
        app.state.store.execute('UPDATE oidc_providers SET required_group=? WHERE id=?',('developers',provider['id']))
    elif kind=='mapping':
        must(b['owner'].post('/api/iam/oidc/providers/'+provider['id']+'/mappings',json={'group_name':'developers','space_id':'team','level':'member'}),201)
    return app.state.oidc


def omit_userinfo_groups(oidc):
    app,_,fake,_,_=oidc
    def handle(request):
        response=fake.handle(request)
        if request.url.path=='/userinfo':return httpx.Response(200,json={'sub':fake.subject})
        return response
    app.state.oidc.transport=httpx.MockTransport(handle)


@pytest.mark.parametrize('policy',['required','mapping'])
@pytest.mark.parametrize('source',['id_token_only','no_userinfo'])
def test_group_policy_rejects_non_reconcilable_login_before_provisioning(oidc,policy,source):
    app,_,fake,_,_=oidc;group_policy(oidc,policy)
    if source=='id_token_only':omit_userinfo_groups(oidc)
    else:fake.discovery_overrides['userinfo_endpoint']=None
    state,response=start(oidc);denied=callback(oidc,state,response)
    assert denied.status_code==403 and denied.json()['error']['code']=='OIDC_GROUPS_UNAVAILABLE'
    assert not app.state.store.all('SELECT id FROM external_identities')


@pytest.mark.parametrize('policy',['required','mapping'])
def test_later_missing_claim_does_not_mean_authoritative_group_removal(oidc,policy):
    service=group_policy(oidc,policy);user,_=login(oidc)
    app=oidc[0];identity=app.state.store.one('SELECT * FROM external_identities')
    before=app.state.store.all('SELECT id_hash FROM sessions WHERE user_id=?',(identity['user_id'],))
    omit_userinfo_groups(oidc)
    with pytest.raises(DevError) as denied:asyncio.run(service.sync_identity(identity['id']))
    assert denied.value.code=='OIDC_GROUPS_UNAVAILABLE'
    after=app.state.store.one('SELECT * FROM external_identities')
    assert after['enabled']==1 and after['fresh_until']==identity['fresh_until']
    assert app.state.store.all('SELECT id_hash FROM sessions WHERE user_id=?',(identity['user_id'],))==before
    app.state.store.execute('UPDATE external_identities SET fresh_until=0')
    assert user.get('/api/projects').status_code==403  # Missing claims never prolong authority.


def test_explicit_empty_groups_still_revokes_required_membership(oidc):
    service=group_policy(oidc,'required');login(oidc)
    app,_,fake,_,_=oidc;identity=app.state.store.one('SELECT * FROM external_identities')
    fake.groups=[]
    with pytest.raises(DevError) as denied:asyncio.run(service.sync_identity(identity['id']))
    assert denied.value.code=='OIDC_ADMISSION_DENIED'
    assert app.state.store.one('SELECT enabled FROM external_identities')['enabled']==0


def test_no_group_policy_does_not_invent_id_token_only_groups(oidc):
    omit_userinfo_groups(oidc);login(oidc)
    assert json.loads(oidc[0].state.store.one('SELECT groups_json FROM external_identities')['groups_json'])==[]


def loaded(oidc):
    app,_,fake,row,_=oidc;service=app.state.oidc
    provider=service.provider(row['id'])
    _,keys=asyncio.run(service.metadata(provider))
    return service,fake,provider,keys


@pytest.mark.parametrize('field,value',[
    ('aud','wrong'),('iss','https://other.test/'),('exp',1),('iat',99999999999),
    ('nonce','wrong'),('azp','wrong'),('sub',''),('at_hash','wrong')])
def test_semantic_id_token_errors_do_not_refetch_discovery_or_keys(oidc,field,value):
    service,fake,provider,keys=loaded(oidc)
    claims={**fake.claims(),field:value};encoded=fake.sign(claims);before=len(fake.requests)
    with pytest.raises(DevError):asyncio.run(service.verify_claims(encoded,provider,keys,nonce='',access_token=fake.access_token))
    assert len(fake.requests)==before


@pytest.mark.parametrize('field,value',[
    ('aud','wrong'),('iss','https://other.test/'),('iat',1),('nonce','forbidden'),('events',{}),('jti','')])
def test_bad_signed_logout_claims_do_not_trigger_forced_refresh(oidc,field,value):
    service,fake,provider,_=loaded(oidc);app,b,_,_,_=oidc
    claims={'iss':fake.issuer,'aud':fake.client_id,'sub':fake.subject,'iat':time.time(),'jti':'logout-test','events':{LOGOUT_EVENT:{}}}
    claims[field]=value;encoded=fake.sign(claims);before=len(fake.requests)
    response=b['bob'].post('/auth/oidc/'+provider['id']+'/backchannel-logout',data={'logout_token':encoded})
    assert response.status_code==401
    assert len(fake.requests)==before


@pytest.mark.parametrize('mode',['unknown_kid','signature'])
def test_key_refresh_is_singleflight_and_rate_limited_across_arbitrary_tokens(oidc,mode):
    service,fake,provider,keys=loaded(oidc);before=len(fake.requests)
    badkey=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    async def attacks():
        tokens=[jwt.encode(fake.claims(),badkey,algorithm='RS256',headers={'kid':f'invented-{n}' if mode=='unknown_kid' else 'key-1'}) for n in range(30)]
        return await asyncio.gather(*(service.verify_claims(value,provider,keys,nonce='') for value in tokens),return_exceptions=True)
    errors=asyncio.run(attacks())
    assert all(isinstance(error,DevError) for error in errors)
    assert len(fake.requests)-before==2  # One discovery/JWKS pair, not 30 pairs.


def test_real_key_rotation_revalidates_with_new_key(oidc):
    service,fake,provider,keys=loaded(oidc)
    newkey=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    newjwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(newkey.public_key()))
    fake.jwk={**newjwk,'kid':'key-2','alg':'RS256','use':'sig'}
    encoded=jwt.encode(fake.claims(),newkey,algorithm='RS256',headers={'kid':'key-2'})
    assert asyncio.run(service.verify_claims(encoded,provider,keys,nonce=''))['sub']==fake.subject


def test_failed_key_refresh_preserves_good_cache_and_cooldown(oidc):
    service,fake,provider,keys=loaded(oidc)
    existing=service.store.one('SELECT * FROM oidc_cache')
    calls=[]
    def broken(request):calls.append(request);return httpx.Response(503,json={})
    service.transport=httpx.MockTransport(broken)
    wrong=jwt.encode(fake.claims(),fake.key,algorithm='RS256',headers={'kid':'unknown'})
    for _ in range(3):
        with pytest.raises(DevError):asyncio.run(service.verify_claims(wrong,provider,keys,nonce=''))
    assert len(calls)==1 and service.store.one('SELECT * FROM oidc_cache')==existing
    assert asyncio.run(service.verify_claims(fake.sign(fake.claims()),provider,keys,nonce=''))['sub']==fake.subject


@pytest.mark.parametrize('encoded',['not-a-token','e30.e30.invalid','',None])
def test_malformed_token_does_not_request_keys(oidc,encoded):
    service,fake,provider,keys=loaded(oidc);before=len(fake.requests)
    with pytest.raises(DevError):asyncio.run(service.verify_claims(encoded,provider,keys))
    assert len(fake.requests)==before


def test_four_network_clients_cannot_fill_global_login_pool_or_evict_victim(oidc):
    service,fake,provider,_=loaded(oidc)
    async def requests():
        _,victim,_=await service.begin(provider,'/',client_key='victim')
        for ip in range(4):
            for _ in range(32):await service.begin(provider,'/',client_key=f'network-{ip}')
            with pytest.raises(DevError) as denied:await service.begin(provider,'/',client_key=f'network-{ip}')
            assert denied.value.code=='OIDC_CLIENT_BUSY'
        assert service.store.one('SELECT * FROM oidc_transactions WHERE state_hash=?',(victim,))
        await service.begin(provider,'/',client_key='legitimate-new-client')
    asyncio.run(requests())
    assert service.store.one('SELECT count(*) AS n FROM oidc_transactions')['n']==130
    assert len(fake.requests)==2  # Admission quota checks do not force metadata I/O.


def test_forwarded_header_is_not_a_login_capacity_identity(oidc,monkeypatch):
    app,b,_,provider,_=oidc
    monkeypatch.setattr(app.state.runtime.oauth,'throttle',lambda request:None)
    codes=[b['bob'].get('/auth/oidc/'+provider['id']+'/start',headers={'X-Forwarded-For':f'198.51.100.{n}'},follow_redirects=False).status_code for n in range(36)]
    assert codes==[303]*32+[429]*4


def test_provider_capacity_and_link_reservation_are_independent(oidc,monkeypatch):
    import hub.oidc as module
    app,b,_,provider,config=oidc
    other=must(b['owner'].post('/api/iam/oidc/providers',json={**config,'issuer':'https://other.test/'}),201)
    service=app.state.oidc
    monkeypatch.setattr(module,'LOGIN_PROVIDER_LIMIT',3)
    monkeypatch.setattr(module,'LOGIN_PUBLIC_LIMIT',4)
    async def metadata(provider,**kwargs):return {'authorization_endpoint':'https://identity.example.test/authorize'},{}
    monkeypatch.setattr(service,'metadata',metadata)
    async def requests():
        for n in range(3):await service.begin(provider,'/',client_key=f'public-{n}')
        with pytest.raises(DevError):await service.begin(provider,'/',client_key='different-ip')
        await service.begin(other,'/',client_key='other-provider')
        with pytest.raises(DevError):await service.begin(other,'/',client_key='different-again')
        session=app.state.store.one('SELECT * FROM sessions WHERE user_id=?',('owner',))
        await service.begin(provider,'/',link_session=session)
    asyncio.run(requests())
    assert app.state.store.one('SELECT count(*) AS n FROM oidc_transactions')['n']==5


def test_failed_discovery_releases_reserved_capacity(oidc,monkeypatch):
    app,_,_,provider,_=oidc;service=app.state.oidc
    async def unavailable(provider,**kwargs):raise DevError('OIDC_UPSTREAM_ERROR','unavailable',502)
    monkeypatch.setattr(service,'metadata',unavailable)
    with pytest.raises(DevError):asyncio.run(service.begin(provider,'/',client_key='network'))
    assert not app.state.store.all('SELECT state_hash FROM oidc_transactions')
