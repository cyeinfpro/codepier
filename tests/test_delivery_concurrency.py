"""Real Store authorization and ordering checks without network or Agent processes."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from hub.auth import Auth
from hub.principal import Principal
from hub.runtime import Connection, Runtime
from hub.store import Store
from shared.contracts import TOOLS
from shared.util import DevError
from tests.legacy_iam_fixture import seed_owner


@pytest.fixture
def delivery(tmp_path):
    store = Store(tmp_path/'hub')
    seed_owner(store, 'owner', 'owner')
    for identifier in ['hot', 'cold']:
        store.execute('INSERT INTO devices(id,name,secret,created) VALUES(?,?,?,?)',
                      (identifier, identifier, store.encrypt('fixture'), time.time()))
        store.execute('''INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,created)
            VALUES(?,?,?,?,?,'write',1,?)''',
            (identifier, identifier, identifier, identifier, '/fixture/'+identifier, time.time()))
    runtime = Runtime(store)
    viewer = Principal('panel:owner', 'owner', {'read','write','execute'}, ['*'], admin=True)
    grant = Auth(store).issue_grant(viewer, 'fixture', ['read'], ['hot','cold'], 30)
    principal = runtime.grant_principal(store.one('SELECT * FROM grants WHERE id=?', (grant['grant_id'],)))
    for identifier in ['hot', 'cold']:
        runtime.connections[identifier] = SimpleNamespace(unusable=False, device_secret=None,
            journal_id='fixture-journal', last_seen=time.time(), tool_protocol=None, cancel_pending_protocol=1)
    yield SimpleNamespace(store=store, runtime=runtime, viewer=viewer, principal=principal, grant=grant)
    store.close()


def admit(e, *, project='hot', key='fixture-read'):
    args = TOOLS['read'].model.model_validate({'project':project,'path':'README.md',
                                              'idempotency_key':key}).model_dump()
    return e.runtime._admit_operation('read', args, e.runtime.project(project,e.principal), e.principal)[0]


@pytest.mark.parametrize('probe', [False, True])
def test_read_only_grant_cannot_admit_provider_probe(delivery, probe):
    e = delivery
    args = TOOLS['computer_status'].model.model_validate({'project':'hot','probe':probe}).model_dump()
    if probe:
        with pytest.raises(DevError) as denied:
            e.runtime._invoke('computer_status', args, e.principal)
        assert denied.value.code == 'INSUFFICIENT_SCOPE'
        with pytest.raises(DevError):
            e.runtime._admit_operation('computer_status', args, e.runtime.project('hot', e.principal), e.principal)
        assert e.store.one('SELECT count(*) AS n FROM operations')['n'] == 0
    else:
        assert e.runtime._invoke('computer_status', args, e.principal).action == 'dispatch'
        assert e.runtime._admit_operation('computer_status', args, e.runtime.project('hot', e.principal), e.principal)[0]


@pytest.mark.parametrize('probe', [False, True])
def test_probe_scope_is_rechecked_on_dispatch_and_receipt(delivery, probe):
    e = delivery
    e.store.execute('UPDATE grants SET scopes=? WHERE id=?', (json.dumps(['read','computer']), e.grant['grant_id']))
    principal = e.runtime.grant_principal(e.store.one('SELECT * FROM grants WHERE id=?', (e.grant['grant_id'],)))
    args = TOOLS['computer_status'].model.model_validate({'project':'hot','probe':probe}).model_dump()
    identifier = e.runtime._admit_operation('computer_status', args, e.runtime.project('hot', principal), principal)[0]
    device, connection, packets = e.runtime._prepare_delivery(identifier)
    row = e.store.one('SELECT * FROM operations WHERE id=?', (identifier,))
    request = json.loads(e.store.decrypt(row['payload']))
    e.store.execute('UPDATE grants SET scopes=? WHERE id=?', (json.dumps(['read']), e.grant['grant_id']))
    current = e.runtime.grant_principal(e.store.one('SELECT * FROM grants WHERE id=?', (e.grant['grant_id'],)))
    assert bool(e.runtime.permission_error(row, request)) is probe
    assert e.runtime._delivery_packet_current(identifier, device, connection, packets[0]) is (not probe)
    for status_only in [False, True]:
        if probe:
            with pytest.raises(DevError):
                e.runtime.operation_row(identifier, current, status_only=status_only)
        else:
            assert e.runtime.operation_row(identifier, current, status_only=status_only)['id'] == identifier


@pytest.mark.parametrize('force', [False, True])
def test_queued_computer_close_rechecks_current_instance_admin(delivery, force):
    e = delivery
    e.store.execute("UPDATE iam_users SET instance_admin=1 WHERE user_id='owner'")
    args = TOOLS['computer_session_close'].model.model_validate({
        'project':'hot', 'force':force, 'session_id':'a'*32,
        'idempotency_key':'queued-close-authority'}).model_dump()
    identifier = e.runtime._admit_operation('computer_session_close', args,
        e.runtime.project('hot', e.viewer), e.viewer)[0]
    device, connection, packets = e.runtime._prepare_delivery(identifier)
    assert packets[0]['project']['_computer_admin'] is True
    # Keep the same user epoch deliberately: current privilege itself is checked,
    # independently of the normal session-epoch revocation protection.
    e.store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='owner'")
    assert e.runtime._delivery_packet_current(identifier,device,connection,packets[0]) is (not force)
    if not force:
        assert packets[0]['project']['_computer_admin'] is False
    else:
        with pytest.raises(DevError) as denied:
            e.runtime._admit_operation('computer_session_close', args,
                e.runtime.project('hot', e.viewer), e.viewer)
        assert denied.value.code == 'COMPUTER_FORCE_DENIED'


def test_candidate_window_is_fair_and_preserves_device_order(delivery):
    e = delivery
    with e.store.transaction():
        for device, count in [('hot', 140), ('cold', 3)]:
            for index in range(count):
                stamp = index if device == 'hot' else 1000+index
                e.store.db.execute('''INSERT INTO operations
                    (id,device_id,project_id,actor,tool,args_summary,fingerprint,state,created,updated)
                    VALUES(?,?,?,'fixture','read','{}','fixture','queued',?,?)''',
                    (device+str(index).zfill(3),device,device,stamp,stamp))
    rows = e.runtime._delivery_candidates(())
    assert len(rows) == 128
    assert [row['device_id'] for row in rows[:4]] == ['hot','cold','hot','cold']
    assert [row['id'] for row in rows if row['device_id']=='hot'] == ['hot'+str(i).zfill(3) for i in range(125)]
    assert {row['device_id'] for row in e.runtime._delivery_candidates(('hot',))} == {'cold'}


def test_equal_timestamp_candidates_keep_admission_order(delivery):
    e = delivery
    with e.store.transaction():
        for identifier in ['z-first', 'a-second', 'm-third']:
            e.store.db.execute("""INSERT INTO operations
                (id,device_id,project_id,actor,tool,args_summary,fingerprint,state,created,updated)
                VALUES(?,'hot','hot','fixture','read','{}','fixture','queued',1,1)""", (identifier,))
    assert [row['id'] for row in e.runtime._delivery_candidates(())] == ['z-first','a-second','m-third']


@pytest.mark.parametrize('change', ['revoked','mapping','device','cancel','accepted','expired'])
def test_prepared_request_is_rechecked_before_wire_send(delivery, change):
    e = delivery
    identifier = admit(e)
    prepared = e.runtime._prepare_delivery(identifier)
    device, connection, packets = prepared
    assert packets[0]['type'] == 'call'
    assert e.store.one('SELECT attempts FROM operations WHERE id=?',(identifier,))['attempts'] == 1
    if change == 'revoked':
        e.store.execute('UPDATE grants SET revoked=1 WHERE id=?',(e.grant['grant_id'],))
    elif change == 'mapping':
        e.store.execute("UPDATE projects SET root='/changed' WHERE id='hot'")
    elif change == 'device':
        e.store.execute("UPDATE devices SET enabled=0 WHERE id='hot'")
    elif change == 'cancel':
        e.store.execute('UPDATE operations SET cancel_requested=1 WHERE id=?',(identifier,))
    elif change == 'accepted':
        e.store.execute('UPDATE operations SET accepted_at=? WHERE id=?',(time.time(),identifier))
    else:
        e.store.execute('UPDATE operations SET deadline=1 WHERE id=?',(identifier,))
    assert e.runtime._delivery_packet_current(identifier,device,connection,packets[0]) is False
    row = e.store.one('SELECT * FROM operations WHERE id=?',(identifier,))
    assert row['attempts'] == 1 and row['state'] == 'queued' and row['result'] is None


def test_final_check_retains_original_vps_binding_after_wire_redaction(delivery, monkeypatch):
    e = delivery
    identifier = admit(e)
    row = e.store.one('SELECT * FROM operations WHERE id=?',(identifier,))
    request = json.loads(e.store.decrypt(row['payload']))
    request['vps_ref'] = {'id':'fixture-ref'}
    e.store.execute('UPDATE operations SET payload=? WHERE id=?',(e.store.encrypt(json.dumps(request)),identifier))
    seen = []
    allowed = True
    def check(request, project):
        seen.append(request.get('vps_ref'))
        return None if allowed else 'fixture binding revoked'
    def transport(request, project):
        return {k:v for k,v in request.items() if k!='vps_ref'}
    monkeypatch.setattr(e.runtime.vps, 'permission_error', check)
    monkeypatch.setattr(e.runtime.vps, 'transport', transport)
    device, connection, packets = e.runtime._prepare_delivery(identifier)
    assert 'vps_ref' not in packets[0]
    allowed = False
    assert not e.runtime._delivery_packet_current(identifier,device,connection,packets[0])
    assert len(seen) >= 2 and all(value=={'id':'fixture-ref'} for value in seen)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['none','revoked','mapping','device','cancel','accepted','expired'])
async def test_connection_lock_wait_cannot_retain_authorization(delivery, monkeypatch, change):
    e = delivery
    sent, packed, checked = [], [], []
    class Socket:
        async def send_text(self, value):
            sent.append(value)
    class Channel:
        def pack(self, value):
            packed.append(value)
            return json.dumps(value)
    connection = Connection(Socket(), Channel())
    connection.journal_id = 'fixture-journal'
    e.runtime.connections['hot'] = connection
    identifier = admit(e)
    prepared = e.runtime._prepare_delivery(identifier)
    original = e.runtime._delivery_packet_current
    def current(*args):
        checked.append(True)
        return original(*args)
    monkeypatch.setattr(e.runtime, '_delivery_packet_current', current)
    await connection.lock.acquire()
    sending = asyncio.create_task(e.runtime._send_prepared(identifier, prepared))
    try:
        await asyncio.sleep(0)
        assert checked == [] and not sending.done()
        if change == 'revoked':
            e.store.execute('UPDATE grants SET revoked=1 WHERE id=?', (e.grant['grant_id'],))
        elif change == 'mapping':
            e.store.execute("UPDATE projects SET root='/changed' WHERE id='hot'")
        elif change == 'device':
            e.store.execute("UPDATE devices SET enabled=0 WHERE id='hot'")
        elif change == 'cancel':
            e.store.execute('UPDATE operations SET cancel_requested=1 WHERE id=?', (identifier,))
        elif change == 'accepted':
            e.store.execute('UPDATE operations SET accepted_at=? WHERE id=?', (time.time(), identifier))
        elif change == 'expired':
            e.store.execute('UPDATE operations SET deadline=1 WHERE id=?', (identifier,))
    finally:
        connection.lock.release()
        await asyncio.wait_for(sending, 3)
    assert checked == [True]
    assert len(sent) == len(packed) == (1 if change == 'none' else 0)
    if sent:
        assert json.loads(sent[0])['id'] == identifier
    assert e.store.one('SELECT attempts FROM operations WHERE id=?', (identifier,))['attempts'] == 1


def test_authorized_prepared_packet_keeps_original_operation(delivery):
    e = delivery
    identifier = admit(e)
    device, connection, packets = e.runtime._prepare_delivery(identifier)
    assert e.runtime._delivery_packet_current(identifier,device,connection,packets[0])
    assert packets[0]['id'] == identifier
    assert packets[0]['execution_policy']['version'] == 2
    assert e.store.one('SELECT attempts FROM operations WHERE id=?',(identifier,))['attempts'] == 1


@pytest.mark.asyncio
async def test_slow_device_does_not_block_a_later_device_tick(delivery, monkeypatch):
    e = delivery
    hot_started, cold_finished, hold = asyncio.Event(), asyncio.Event(), asyncio.Event()
    first = True
    def candidates(busy):
        nonlocal first
        if first:
            first = False
            return [{'id':'hot-op','device_id':'hot'}]
        assert 'hot' in busy
        return [] if 'cold' in busy or cold_finished.is_set() else [{'id':'cold-op','device_id':'cold'}]
    async def deliver(identifiers):
        if identifiers == ['hot-op']:
            hot_started.set()
            await hold.wait()
        else:
            cold_finished.set()
    monkeypatch.setattr(e.runtime, '_delivery_candidates', candidates)
    monkeypatch.setattr(e.runtime, 'deliver_device', deliver)
    loop = asyncio.create_task(e.runtime.delivery_loop())
    try:
        await asyncio.wait_for(hot_started.wait(), 3)
        e.runtime.wake.set()
        await asyncio.wait_for(cold_finished.wait(), 3)
        assert not hold.is_set()
    finally:
        e.runtime.stopping = True
        e.runtime.wake.set()
        await asyncio.wait_for(loop, 3)
