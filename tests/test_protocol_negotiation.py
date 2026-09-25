"""Delivery-v2 legacy/current wire matrix with real crypto, journal and file writes.

Legacy cases freeze the pre-negotiation hello/ready/call shape. This is wire-level
compatibility evidence, not a claim that every historical binary was executed.
"""
import copy
import json
import time
from types import SimpleNamespace
import pytest
from shared.crypto import SecureChannel, token
from shared.tool_protocol import advertisement, catalog_digest, negotiate, require_compatible, validate_call_epoch
from shared.util import DevError
from agent.runner import Agent
from hub.runtime import Runtime, Principal
from hub.store import Store


@pytest.mark.parametrize('old_hub', [True, False], ids=['legacy-hub-wire', 'current-hub-wire'])
@pytest.mark.parametrize('old_agent', [True, False], ids=['legacy-agent-wire', 'current-agent-wire'])
@pytest.mark.asyncio
async def test_bidirectional_wire_matrix_preserves_one_write_and_recovery(tmp_path, old_hub, old_agent):
    root = tmp_path / 'project';root.mkdir()
    secret = token(32)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'hub_url':'127.0.0.1:9','device_id':'dev','secret':secret,'state_dir':str(tmp_path/'state'),
        'allowed_roots':[{'path':str(root),'writable':True,'allow_tasks':True}],'tasks':{}}))
    agent = Agent(config)
    store = Store(tmp_path / 'hub')
    runtime = Runtime(store);runtime.wait_seconds = 0
    try:
        store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('dev','fixture',?,?)", (store.encrypt(secret), time.time()))
        store.execute("INSERT INTO projects VALUES ('proj','Fixture','fixture','dev',?,'','write',1,?)", (str(root), time.time()))
        principal = Principal('panel:owner','owner',{'read','write','execute'},['*'],admin=True)
        challenge = token(32)
        hub_channel = SecureChannel(secret, challenge, 'dev', 'hub')
        agent_channel = SecureChannel(secret, challenge, 'dev', 'agent')
        hello = {'type':'hello','version':'1.13.0','journal_id':agent.journal.journal_id,'delivery_protocol':2}
        if not old_agent: hello.update(advertisement(agent.build.catalog_sha256))
        hello = hub_channel.unpack(agent_channel.pack(hello))
        assert hello['delivery_protocol'] == 2
        peer = SimpleNamespace(last_seen=time.time(), journal_id=hello['journal_id'], unusable=False,
                               tool_protocol=None if old_hub else negotiate(hello))
        ready = {'type':'ready','delivery_protocol':2}
        if not old_hub: ready.update(advertisement(catalog_digest()))
        ready = agent_channel.unpack(hub_channel.pack(ready))
        if not old_agent: assert negotiate(ready).legacy == old_hub
        packets = []
        async def report(event):
            packet = hub_channel.unpack(agent_channel.pack(event))
            ack = await store.run(runtime._receive_operation_event, 'dev', peer, packet)
            if ack:
                decoded = agent_channel.unpack(hub_channel.pack(ack))
                agent.journal.ack(decoded['id'])
            return True
        async def deliver(packet):
            outgoing = dict(packet)
            if old_hub: outgoing.pop('tool_contract_version', None)
            received = agent_channel.unpack(hub_channel.pack(outgoing))
            packets.append(copy.deepcopy(received))
            if old_agent: received.pop('tool_contract_version', None)
            if received['type'] == 'call': await agent.handle(received)
            elif received['type'] == 'probe': await agent.report_status(received['id'])
        peer.send = deliver;agent.send = report
        args = {'project':'Fixture','path':'once.txt','content':'only once','expected_sha256':'new','idempotency_key':'wire-matrix'}
        receipt = await runtime.invoke('fs_write', args, principal)
        runtime.connections['dev'] = peer
        await runtime.deliver(receipt['operation_id'])
        assert (root / 'once.txt').read_text() == 'only once'
        assert runtime.operation(receipt['operation_id'], principal)['state'] == 'succeeded'
        await deliver(packets[0])
        assert len(agent.journal.history(str(root), 'once.txt')) == 1
        repeated = await runtime.invoke('fs_write', args, principal)
        assert repeated['operation_id'] == receipt['operation_id']
    finally:
        await store.aclose()
        agent.journal.db.close();agent.instance_lock.close()


@pytest.mark.parametrize('problem', ['protocol','boolean_protocol','missing_version','old_version','hash','tools','boolean_epoch','tool_name'])
def test_malformed_advertisements_fail_closed(problem):
    frame = advertisement('a' * 64)
    if problem == 'protocol': frame['contract_protocol'] = 2
    elif problem == 'boolean_protocol': frame['contract_protocol'] = True
    elif problem == 'missing_version': frame.pop('version')
    elif problem == 'old_version': frame['version'] = '0.9.0'
    elif problem == 'hash': frame['catalog_sha256'] = 'invalid'
    elif problem == 'tools': frame['tool_contracts'] = []
    elif problem == 'boolean_epoch': frame['tool_contracts']['fs_write'] = True
    elif problem == 'tool_name': frame['tool_contracts']['../escape'] = 1
    with pytest.raises(ValueError): negotiate(frame)


def test_additive_catalog_difference_is_not_a_breaking_tool_change():
    frame = advertisement('b' * 64);frame['tool_contracts']['future_read'] = 1
    peer = negotiate(frame)
    require_compatible(peer, 'fs_write')
    assert peer.public('a' * 64)['catalog_matches'] is False
    frame['tool_contracts']['fs_write'] = 2
    with pytest.raises(DevError): require_compatible(negotiate(frame), 'fs_write')


def test_minimum_release_and_legacy_epoch_cannot_be_bypassed(monkeypatch):
    from shared import tool_protocol
    legacy = negotiate({'version':'1.13.0'})
    require_compatible(legacy, 'fs_write')
    monkeypatch.setitem(tool_protocol.TOOL_MINIMUM_RELEASES, 'fs_write', (1, 14, 0))
    with pytest.raises(DevError): require_compatible(legacy, 'fs_write')
    monkeypatch.setitem(tool_protocol.TOOL_WIRE_VERSIONS, 'fs_write', 2)
    with pytest.raises(DevError): validate_call_epoch('fs_write', None)
    validate_call_epoch('fs_write', 2)


@pytest.mark.parametrize('epoch', [True, 0, 2, '1'])
def test_invalid_call_epoch_is_rejected(epoch):
    with pytest.raises(DevError): validate_call_epoch('fs_write', epoch)


@pytest.mark.asyncio
@pytest.mark.parametrize('sent', [False, True])
async def test_pending_payload_keeps_original_semantics_after_upgrade(tmp_path, monkeypatch, sent):
    from shared import tool_protocol
    store = Store(tmp_path / 'hub')
    runtime = Runtime(store);runtime.wait_seconds = 0
    try:
        store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('dev','fixture',?,?)", (store.encrypt(token()), time.time()))
        store.execute("INSERT INTO projects VALUES ('proj','Fixture','fixture','dev','/fixture','','write',1,?)", (time.time(),))
        principal = Principal('panel:owner','owner',{'read','write','execute'},['*'],admin=True)
        receipt = await runtime.invoke('fs_write', {'project':'Fixture','path':'once','content':'one','expected_sha256':'new','idempotency_key':'epoch-fence'}, principal)
        row = store.one('SELECT payload FROM operations WHERE id=?', (receipt['operation_id'],))
        assert json.loads(store.decrypt(row['payload']))['tool_contract_version'] == 1
        packets = []
        async def send(packet): packets.append(packet)
        runtime.connections['dev'] = SimpleNamespace(last_seen=time.time(), journal_id='journal', unusable=False, send=send)
        if sent:
            await runtime.deliver(receipt['operation_id'])
            assert packets[-1]['type'] == 'call'
            packets.clear()
        monkeypatch.setitem(tool_protocol.TOOL_WIRE_VERSIONS, 'fs_write', 2)
        await runtime.deliver(receipt['operation_id'])
        operation = runtime.operation(receipt['operation_id'], principal)
        if sent:
            assert packets == [{'type':'probe','id':receipt['operation_id']}]
            assert operation['state'] in {'queued','reconnecting'}
        else:
            assert not packets and operation['state'] == 'failed'
            assert operation['result']['error']['code'] == 'TOOL_PROTOCOL_MISMATCH'
    finally:
        await store.aclose()
