"""Real Store/Runtime admission, with no delivery worker or OS service changes."""
import json
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from hub.panel_agent_rollout import PanelAgentRollouts
from hub.principal import Principal
from hub.runtime import Runtime
from hub.store import Store
from shared.tool_protocol import PeerContract, advertisement, negotiate
from shared.util import VERSION, DevError


class Bridge:
    def __init__(self):
        self.job = {'id': 'a' * 32, 'kind': 'apply', 'state': 'succeeded', 'phase': 'done',
                    'commit_decided': True, 'target_version': VERSION, 'request_key': 'rollout-test-key'}
        self.extra = {}

    async def request(self, method, path, body=None):
        return {'request_found': True, 'operation': self.job, 'current_version': VERSION,
                'busy': False, 'recovery_required': False, **self.extra}


@pytest_asyncio.fixture
async def rig(tmp_path):
    store = Store(tmp_path / 'hub')
    store.execute("INSERT INTO users VALUES ('owner','owner','fixture-only',1)")
    store.execute("UPDATE iam_users SET instance_admin=1 WHERE user_id='owner'")
    runtime = Runtime(store)
    principal = Principal('panel:owner', 'owner', set(), [], user_epoch=1)
    bridge = Bridge()
    rollout = PanelAgentRollouts(runtime, bridge)
    value = SimpleNamespace(store=store, runtime=runtime, principal=principal, bridge=bridge, rollout=rollout,
                            path=tmp_path / 'hub')
    yield value
    for future in value.runtime.futures.values():
        future.cancel()
    value.store.close()


def device(rig, identifier='d1', version='1.0.0', online=True, space='legacy'):
    info = {'version': version, 'device_actions': ['agent_update'],
            'management': {'managed': True, 'service': True, 'status': 'ready', 'last_error': ''}}
    rig.store.execute("""INSERT INTO devices(id,name,secret,info,created,space_id,owner_user_id)
        VALUES (?,?,?,?,?,?,?)""", (identifier, 'Synthetic ' + identifier, rig.store.encrypt('fixture-device'),
                                    json.dumps(info), time.time(), space, 'owner'))
    if online:
        connect(rig, identifier)
    return identifier


def connect(rig, identifier):
    rig.runtime.connections[identifier] = SimpleNamespace(
        unusable=False, last_seen=time.time(), tool_protocol=negotiate(advertisement('a' * 64)))


def prepare(rig):
    body = {'idempotency_key': 'rollout-test-key', 'version': VERSION, 'release_id': 17,
            'sha256': 'b' * 64, 'update_agents': True}
    rig.rollout.prepare(body, rig.principal)
    return body


async def view(rig):
    return await rig.store.run(rig.rollout.view, rig.principal)


def operations(rig):
    return rig.store.all("SELECT * FROM operations WHERE tool='agent_update'")


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['failed', 'rolled_back', 'recovery_required', 'running'])
async def test_panel_failure_or_incomplete_health_never_updates_agents(rig, state):
    device(rig); prepare(rig)
    rig.bridge.job['state'] = state
    await rig.rollout.tick(); await rig.rollout.tick()
    assert operations(rig) == []
    assert (await view(rig))['state'] == ('waiting_panel' if state == 'running' else 'blocked')


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['commit', 'version', 'busy', 'request', 'kind'])
async def test_exact_success_gate(rig, change):
    device(rig); prepare(rig)
    if change == 'commit': rig.bridge.job['commit_decided'] = False
    if change == 'version': rig.bridge.extra['current_version'] = '0.0.0'
    if change == 'busy': rig.bridge.extra['busy'] = True
    if change == 'request': rig.bridge.job['request_key'] = 'other-request'
    if change == 'kind': rig.bridge.job['kind'] = 'check'
    await rig.rollout.tick()
    assert operations(rig) == []
    assert (await view(rig))['state'] == 'waiting_panel'


@pytest.mark.asyncio
async def test_original_receipt_recovers_after_database_reopen_and_repeated_callback(rig):
    device(rig)
    body = prepare(rig)
    await rig.rollout.tick()
    assert len(operations(rig)) == 1
    identifier = operations(rig)[0]['id']
    assert rig.rollout.prepare(body, rig.principal) is False
    await rig.rollout.tick()
    for future in rig.runtime.futures.values(): future.cancel()
    for _ in range(3):
        rig.store.close()
        rig.store = Store(rig.path)
        rig.runtime = Runtime(rig.store)
        connect(rig, 'd1')
        rig.rollout = PanelAgentRollouts(rig.runtime, rig.bridge)
        await rig.rollout.tick()
        assert [op['id'] for op in operations(rig)] == [identifier]
    rig.store.execute("UPDATE operations SET state='succeeded',updated=? WHERE id=?", (time.time(), identifier))
    await rig.rollout.tick()
    assert (await view(rig))['nodes'][0]['state'] == 'awaiting_restart'
    record = rig.store.one("SELECT info FROM devices WHERE id='d1'")
    info = json.loads(record['info']); info['version'] = VERSION
    rig.store.execute("UPDATE devices SET info=?,last_seen=? WHERE id='d1'", (json.dumps(info), time.time()))
    await rig.rollout.tick()
    assert (await view(rig))['state'] == 'succeeded'
    assert len(operations(rig)) == 1


@pytest.mark.asyncio
async def test_crash_between_admission_and_receipt_save_does_not_duplicate(rig, monkeypatch):
    device(rig); prepare(rig)
    original = rig.runtime.dispatch_device_action
    async def interrupted(*args, **kwargs):
        await original(*args, **kwargs)
        raise OSError('simulated receipt loss')
    monkeypatch.setattr(rig.runtime, 'dispatch_device_action', interrupted)
    await rig.rollout.tick()
    assert len(operations(rig)) == 1
    monkeypatch.setattr(rig.runtime, 'dispatch_device_action', original)
    await rig.rollout.tick()
    assert len(operations(rig)) == 1
    assert (await view(rig))['nodes'][0]['operation_id'] == operations(rig)[0]['id']


@pytest.mark.asyncio
async def test_offline_then_busy_drains_without_killing_original_task(rig):
    device(rig, online=False); prepare(rig)
    await rig.rollout.tick()
    assert (await view(rig))['nodes'][0]['state'] == 'offline'
    connect(rig, 'd1')
    rig.store.execute("""INSERT INTO operations(id,device_id,actor,tool,args_summary,fingerprint,state,created,updated)
        VALUES ('business-write','d1','panel:owner','fs_write','{}','fixture','running',1,1)""")
    await rig.rollout.tick()
    assert (await view(rig))['nodes'][0]['state'] == 'waiting_idle'
    assert rig.store.one("SELECT state FROM operations WHERE id='business-write'")['state'] == 'running'
    assert not operations(rig)
    rig.store.execute("UPDATE operations SET state='succeeded' WHERE id='business-write'")
    await rig.rollout.tick()
    assert len(operations(rig)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('revocation', ['user', 'device', 'space', 'admin', 'protocol', 'capability'])
async def test_live_revocation_and_incompatibility_stop_dispatch(rig, revocation):
    device(rig); prepare(rig)
    if revocation == 'user':
        rig.store.execute("UPDATE iam_users SET epoch=epoch+1 WHERE user_id='owner'")
    elif revocation == 'device':
        rig.store.execute("UPDATE devices SET enabled=0 WHERE id='d1'")
    elif revocation == 'space':
        rig.store.execute("UPDATE memberships SET active=0 WHERE user_id='owner'")
    elif revocation == 'admin':
        rig.store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='owner'")
    elif revocation == 'protocol':
        rig.runtime.connections['d1'].tool_protocol = PeerContract((1, 0, 0), False, 'a' * 64, {'agent_update': 99})
    else:
        info = json.loads(rig.store.one("SELECT info FROM devices WHERE id='d1'")['info'])
        info['device_actions'] = []
        rig.store.execute("UPDATE devices SET info=? WHERE id='d1'", (json.dumps(info),))
    await rig.rollout.tick()
    assert not operations(rig)
    row = rig.store.one('SELECT state,nodes FROM panel_agent_rollouts')
    assert row['state'] == 'partial_failure'
    assert json.loads(row['nodes'])[0]['state'] == 'blocked'


@pytest.mark.asyncio
async def test_snapshot_never_expands_to_new_or_other_space_devices(rig):
    device(rig)
    rig.store.execute("INSERT INTO spaces(id,label,kind,created) VALUES ('other','Other','team',1)")
    device(rig, 'other-device', space='other')
    prepare(rig)
    device(rig, 'new-device')
    await rig.rollout.tick()
    assert [op['device_id'] for op in operations(rig)] == ['d1']
    assert [node['device_id'] for node in (await view(rig))['nodes']] == ['d1']


@pytest.mark.asyncio
@pytest.mark.parametrize('current,state', [(VERSION, 'skipped'), ('999.0.0', 'skipped'), ('bad', 'blocked')])
async def test_equal_newer_or_unknown_versions_never_get_downgraded(rig, current, state):
    device(rig, version=current); prepare(rig)
    await rig.rollout.tick()
    assert not operations(rig)
    assert (await view(rig))['nodes'][0]['state'] == state


@pytest.mark.asyncio
async def test_partial_failure_and_agent_rollback_are_terminal(rig):
    device(rig, 'good', version=VERSION); device(rig, 'bad')
    prepare(rig); await rig.rollout.tick()
    op = operations(rig)[0]
    rig.store.execute("UPDATE operations SET state='succeeded',updated=? WHERE id=?", (time.time(), op['id']))
    info = json.loads(rig.store.one("SELECT info FROM devices WHERE id='bad'")['info'])
    info['management'].update(status='rollback', last_error='Synthetic startup failed; restored old runtime')
    rig.store.execute("UPDATE devices SET info=?,last_seen=? WHERE id='bad'", (json.dumps(info), time.time()))
    await rig.rollout.tick(); await rig.rollout.tick()
    result = await view(rig)
    assert result['state'] == 'partial_failure'
    assert {node['state'] for node in result['nodes']} == {'failed', 'skipped'}
    assert 'restored old runtime' in result['nodes'][0]['message']
    assert len(operations(rig)) == 1


@pytest.mark.asyncio
async def test_conflicting_body_never_replaces_authorized_snapshot(rig):
    device(rig); body = prepare(rig)
    with pytest.raises(DevError, match='更新编号'):
        rig.rollout.prepare({**body, 'version': '999.0.0'}, rig.principal)
    assert len(rig.store.all('SELECT * FROM panel_agent_rollouts')) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('after_snapshot',[False,True])
async def test_consent_and_snapshot_crash_is_atomic_and_incomplete_record_fails_closed(rig,monkeypatch,after_snapshot):
    import hashlib
    device(rig)
    body={'idempotency_key':'atomic-consent-key','version':VERSION,'release_id':17,
          'sha256':'b'*64,'update_agents':True}
    original=rig.rollout.prepare
    def fail(*args):
        if after_snapshot: original(*args)
        raise OSError('fault before atomic confirmation commit')
    monkeypatch.setattr(rig.rollout,'prepare',fail)
    with pytest.raises(OSError):
        await rig.store.run(rig.rollout.confirm,body,rig.principal)
    assert rig.store.all('SELECT * FROM panel_update_consents')==[]
    assert rig.store.all('SELECT * FROM panel_agent_rollouts')==[]
    monkeypatch.setattr(rig.rollout,'prepare',original)
    digest=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
    rig.store.execute('INSERT INTO panel_update_consents VALUES (?,?,?,?)',
                      (body['idempotency_key'],digest,'owner','legacy'))
    device(rig,'added-later')
    with pytest.raises(DevError) as error:
        await rig.store.run(rig.rollout.confirm,body,rig.principal)
    assert error.value.code=='AGENT_CONSENT_INCOMPLETE'
    assert rig.store.all('SELECT * FROM panel_agent_rollouts')==[]


@pytest.mark.asyncio
async def test_concurrent_same_key_confirmation_keeps_one_original_snapshot(rig):
    import asyncio
    device(rig)
    body={'idempotency_key':'concurrent-consent-key','version':VERSION,'release_id':17,
          'sha256':'b'*64,'update_agents':True}
    results=await asyncio.gather(*(rig.store.run(rig.rollout.confirm,body,rig.principal) for _ in range(3)))
    assert results.count(True)==1 and results.count(False)==2
    device(rig,'added-later')
    assert await rig.store.run(rig.rollout.confirm,body,rig.principal) is False
    rows=rig.store.all('SELECT nodes FROM panel_agent_rollouts')
    assert len(rows)==1
    assert [node['device_id'] for node in json.loads(rows[0]['nodes'])]==['d1']


@pytest.mark.asyncio
async def test_failed_generation_is_not_replayed_by_restart_or_new_key(rig):
    device(rig); prepare(rig)
    rig.bridge.job['state']='rolled_back'
    await rig.rollout.tick()
    for _ in range(3):
        rig.rollout=PanelAgentRollouts(rig.runtime,rig.bridge)
        await rig.rollout.tick()
    assert operations(rig)==[]
    rig.bridge.job.update(state='succeeded',request_key='new-explicit-key')
    # A new host generation alone cannot invent a new local authorization.
    await rig.rollout.tick()
    assert operations(rig)==[]
