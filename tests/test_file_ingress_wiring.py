"""Ingress wiring contracts with synthetic files, identities and offline transport."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from jsonschema import Draft202012Validator

from agent.incoming_uploads import IncomingUploads
from agent.runner import Agent
from hub.incoming_files import IncomingFileService
from hub.principal import Principal
from hub.runtime import Runtime
from hub.store import Store
from hub.tool_router import ToolRouter
from scripts import file_import_client as adapter
from scripts import mcp_stdio_bridge as bridge
from shared.contracts import TOOLS, tool_definitions
from shared.crypto import token
from shared.integration_contracts import INCOMING_UPLOAD_TOOLS
from shared.util import DevError, atomic_json, safe_summary
from tests.legacy_iam_fixture import seed_grant, seed_owner

PRIVATE_BYTES = b'PRIVATE_BINARY_PAYLOAD_opaque'
PRIVATE_TOKEN = 'rd_' + 'S' * 43
UPLOAD = 'a' * 32
DESTINATION = 'assets/imported.opaque'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def local_request(source, /, **changes):
    return {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': bridge.LOCAL_UPLOAD_TOOL, 'arguments': {
                'source': str(source), 'project': 'project-a',
                'destination': DESTINATION, 'idempotency_key': 'wiring-upload-key',
                **changes}}}


@pytest.fixture(autouse=True)
def synthetic_environment(monkeypatch):
    # Never read this executor's credential or inherit an ingress opt-in.
    for key in ('CODEPIER_TOKEN_FILE', 'REMOTE_DEV_TOKEN_FILE',
                'CODEPIER_FILE_IMPORT_ROOTS', 'CODEPIER_FILE_IMPORT_STREAMING'):
        monkeypatch.delenv(key, raising=False)


def test_local_upload_default_is_not_advertised():
    original = {'jsonrpc': '2.0', 'id': 1, 'result': {'tools': [{'name': 'read'}]}}
    for raw in ('', None, '[]'):
        roots = bridge.local_upload_roots(raw)
        assert roots == []
        payload = copy.deepcopy(original)
        assert bridge.add_local_upload_tool(payload, roots, {'method': 'tools/list'}) == original


def test_local_upload_roots_are_explicit_absolute_json_paths(tmp_path):
    roots = [tmp_path / 'shared files', tmp_path / '资料']
    assert bridge.local_upload_roots(json.dumps(list(map(str, roots)))) == roots
    definition = bridge.local_upload_definition()
    Draft202012Validator.check_schema(definition['inputSchema'])
    Draft202012Validator(definition['inputSchema']).validate(
        local_request(tmp_path / 'file')['params']['arguments'])
    assert definition['name'] == bridge.LOCAL_UPLOAD_TOOL
    assert definition['annotations']['readOnlyHint'] is False
    assert definition['annotations']['destructiveHint'] is False


@pytest.mark.parametrize('raw', ['not-json', '{}', 'null', 'true', '1', '"/tmp"',
                                 '["relative"]', '[""]', '[null]', '[1]',
                                 json.dumps(['/tmp'] * 33)])
def test_invalid_local_upload_root_configuration_fails_closed(raw):
    with pytest.raises(ValueError):
        bridge.local_upload_roots(raw)


def test_local_tool_is_only_added_once_and_never_on_a_cursor_page(tmp_path):
    roots = [tmp_path]
    payload = {'result': {'tools': [{'name': 'read'}]}}
    result = bridge.add_local_upload_tool(payload, roots, {'method': 'tools/list'})
    assert [tool['name'] for tool in result['result']['tools']] == ['read', bridge.LOCAL_UPLOAD_TOOL]
    before = copy.deepcopy(result)
    with pytest.raises(ValueError, match='collides'):
        bridge.add_local_upload_tool(result, roots, {'method': 'tools/list'})
    assert result == before

    next_page = {'result': {'tools': [{'name': 'remote-tool'}]}}
    before = copy.deepcopy(next_page)
    assert bridge.add_local_upload_tool(
        next_page, roots, {'method': 'tools/list', 'params': {'cursor': 'opaque'}}) == before


def run_bridge_main(monkeypatch, capsys, tmp_path, requests, transport, *, roots=None):
    """Run the actual stdio loop; only HTTP, token storage and stdin are synthetic."""
    client = httpx.Client(transport=httpx.MockTransport(transport), trust_env=False)
    monkeypatch.setattr(bridge.httpx, 'Client', lambda **kwargs: client)
    monkeypatch.setattr(bridge, 'token_from_file', lambda path: PRIVATE_TOKEN)
    monkeypatch.setattr(bridge.sys, 'stdin', SimpleNamespace(
        buffer=io.BytesIO(('\n'.join(json.dumps(item) for item in requests) + '\n').encode())))
    monkeypatch.setenv('CODEPIER_TOKEN_FILE', str(tmp_path / 'synthetic-token'))
    monkeypatch.setenv('CODEPIER_HUB_URL', 'https://hub.example')
    monkeypatch.setenv('CODEPIER_MCP_PROFILE', 'core')
    if roots is not None:
        monkeypatch.setenv('CODEPIER_FILE_IMPORT_ROOTS', json.dumps(list(map(str, roots))))
    status = bridge.main()
    captured = capsys.readouterr()
    return status, [json.loads(line) for line in captured.out.splitlines()], captured


def test_default_local_call_is_rejected_without_remote_forwarding(monkeypatch, capsys, tmp_path):
    traffic = []

    def remote(request):
        traffic.append(request)
        wire = json.loads(request.content)
        assert wire['method'] == 'tools/list'
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': wire['id'],
                                        'result': {'tools': [{'name': 'read'}]}})

    requests = [{'jsonrpc': '2.0', 'id': 0, 'method': 'tools/list'},
                local_request(tmp_path / 'private-source')]
    status, replies, captured = run_bridge_main(monkeypatch, capsys, tmp_path, requests, remote)
    assert status == 0 and len(traffic) == 1
    assert [item['name'] for item in replies[0]['result']['tools']] == ['read']
    assert replies[1]['result']['isError'] is True
    assert replies[1]['result']['structuredContent']['error']['code'] == 'LOCAL_UPLOAD_DISABLED_OR_INVALID'
    assert str(tmp_path) not in captured.out + captured.err
    assert PRIVATE_TOKEN not in captured.out + captured.err


@pytest.mark.skipif(os.name != 'posix', reason='Local safe-handle client requires POSIX')
def test_local_main_streams_binary_outside_rpc_and_returns_metadata_only(
        monkeypatch, capsys, tmp_path):
    source = tmp_path / 'PRIVATE_LOCAL_NAME.opaque'
    source.write_bytes(PRIVATE_BYTES)
    received = 0
    complete = False
    traffic = []

    def remote(request):
        nonlocal received, complete
        traffic.append(request)
        assert request.url.path != '/mcp'
        assert str(source) not in str(request.url)
        if request.url.path == '/api/file-imports':
            body = json.loads(request.content)
            assert body == {'project': 'project-a', 'path': DESTINATION,
                            'size': len(PRIVATE_BYTES), 'sha256': sha(PRIVATE_BYTES),
                            'workspace_id': '', 'idempotency_key': 'wiring-upload-key'}
            operation = UPLOAD
        elif request.url.path.endswith('/chunks'):
            assert request.headers['Content-Type'] == 'application/octet-stream'
            assert request.content == PRIVATE_BYTES
            received += len(request.content)
            operation = 'b' * 32
        elif request.url.path.endswith('/finish'):
            complete = True
            operation = 'f' * 32
        else:
            assert request.method == 'GET'
            operation = 'c' * 32
        return httpx.Response(200, json={
            'operation_id': operation, 'upload_id': UPLOAD, 'path': DESTINATION,
            'bytes': len(PRIVATE_BYTES), 'received': received, 'sha256': sha(PRIVATE_BYTES),
            'state': 'complete' if complete else 'receiving', 'created': complete,
            'ready': complete, 'expires': time.time() + 3600,
            # Unexpected peer fields must be removed before reaching the model.
            'source': str(source), 'data': PRIVATE_BYTES.decode(), 'token': PRIVATE_TOKEN,
        })

    status, replies, captured = run_bridge_main(
        monkeypatch, capsys, tmp_path, [local_request(source)], remote, roots=[tmp_path])
    assert status == 0 and len(traffic) == 4 and received == len(PRIVATE_BYTES)
    result = replies[0]['result']
    assert result['isError'] is False
    receipt = result['structuredContent']
    assert receipt['created'] and receipt['ready'] and receipt['sha256'] == sha(PRIVATE_BYTES)
    assert set(receipt) == {'upload_id', 'path', 'bytes', 'received', 'sha256',
                            'state', 'expires', 'created', 'ready', 'operation_id'}
    for private in (str(source), source.name, PRIVATE_BYTES.decode(), PRIVATE_TOKEN):
        assert private not in captured.out + captured.err


@pytest.mark.skipif(os.name != 'posix', reason='Local safe-handle client requires POSIX')
def test_http_rejection_body_and_local_path_are_not_exposed(monkeypatch, capsys, tmp_path):
    source = tmp_path / 'PRIVATE_LOCAL_NAME.opaque'
    source.write_bytes(PRIVATE_BYTES)
    traffic = []

    def reject(request):
        traffic.append(request)
        return httpx.Response(403, json={'error': {
            'code': 'FORBIDDEN', 'message': str(source) + PRIVATE_BYTES.decode() + PRIVATE_TOKEN}})

    status, replies, captured = run_bridge_main(
        monkeypatch, capsys, tmp_path, [local_request(source)], reject, roots=[tmp_path])
    assert status == 0 and len(traffic) == 1
    error = replies[0]['result']['structuredContent']['error']
    assert error['code'] == 'HTTP_403' and error['retryable'] is False
    for private in (str(source), source.name, PRIVATE_BYTES.decode(), PRIVATE_TOKEN):
        assert private not in captured.out + captured.err


def test_local_read_denial_does_not_forward_or_echo_the_source(tmp_path):
    request = local_request(tmp_path / 'PRIVATE_LOCAL_NAME.opaque')
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: pytest.fail('A denied local source must not reach the network'))) as client:
        result = bridge.handle_local_upload(client, 'https://hub.example', request,
            {'Authorization': 'Bearer ' + PRIVATE_TOKEN}, [tmp_path / 'other'], 1)
    error = result['result']['structuredContent']['error']
    assert error['code'] == 'LOCAL_READ_DENIED' and error['retryable'] is False
    assert str(tmp_path) not in json.dumps(result) and 'PRIVATE_LOCAL_NAME' not in json.dumps(result)


@pytest.mark.parametrize('changes', [
    {'workspace_id': 'not-an-id'}, {'idempotency_key': 'short'},
    {'unrequested': 'raw'}, {'source': 123}, {'destination': ''},
])
def test_invalid_local_call_never_reaches_adapter(monkeypatch, tmp_path, changes):
    monkeypatch.setattr(adapter, 'upload_local_file',
                        lambda *args, **kwargs: pytest.fail('Invalid local call reached adapter'))
    result = bridge.handle_local_upload(object(), 'https://hub.example',
        local_request(tmp_path / 'source', **changes), {}, [tmp_path], 1)
    assert result['result']['structuredContent']['error']['code'] == 'LOCAL_UPLOAD_DISABLED_OR_INVALID'


def test_safe_local_error_retains_exact_recovery_ids(monkeypatch, tmp_path):
    def pending(*args, **kwargs):
        raise adapter.FileImportError('IMPORT_PENDING', 'Recover the exact operation',
                                     retryable=True, operation_id='b' * 32, upload_id=UPLOAD)
    monkeypatch.setattr(adapter, 'upload_local_file', pending)
    result = bridge.handle_local_upload(object(), 'https://hub.example',
        local_request(tmp_path / 'private-source'), {}, [tmp_path], 1)
    error = result['result']['structuredContent']['error']
    assert error == {'code': 'IMPORT_PENDING', 'message': 'Recover the exact operation',
                     'retryable': True, 'operation_id': 'b' * 32, 'upload_id': UPLOAD}
    assert str(tmp_path) not in json.dumps(result)


@pytest.fixture
def agent(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    config_path = tmp_path / 'agent-config.json'
    atomic_json(config_path, {'hub_url': 'http://127.0.0.1:9', 'device_id': 'fixture',
        'secret': token(), 'state_dir': str(tmp_path / 'agent-state'),
        'allowed_roots': [{'path': str(root), 'writable': True}], 'tasks': {}})
    instance = Agent(config_path)
    project = {'id': 'project-a', 'root': str(root), 'mode': 'write',
               '_coding_owner': 'synthetic-owner', '_coding_device': 'fixture',
               '_coding_scopes': ['write'], '_workspace_id': ''}
    yield instance, project, root
    instance.journal.db.close()
    instance.instance_lock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit_disabled', [False, True])
async def test_startup_discovers_and_cleans_expired_spool_without_ingress_rpc(
        agent, monkeypatch, explicit_disabled):
    instance, project, root = agent
    if explicit_disabled:
        instance.config.setdefault('integrations', {})['file_import_streaming'] = False
    original = copy.deepcopy(instance.config)
    uploads = IncomingUploads(instance.engine)
    identifier = uuid.uuid4().hex
    uploads.begin(identifier, project, {
        'path': 'never-published.bin', 'size': 5, 'sha256': sha(b'abcde')})
    uploads.chunk(project, {'upload_id': identifier, 'offset': 0,
        'data': b'ab', 'chunk_sha256': sha(b'ab')})
    spool = instance.journal.directory / 'incoming-upload' / (identifier + '.part')
    assert spool.read_bytes() == b'ab'
    with instance.journal.db:
        instance.journal.db.execute(
            'UPDATE incoming_uploads SET expires=0,spool_expires=0 WHERE id=?', (identifier,))
    assert instance.integrations.incoming_uploads is None
    send = AsyncMock(side_effect=AssertionError('Startup maintenance cannot send an RPC'))
    monkeypatch.setattr(instance, 'send', send)
    try:
        await instance.integrations.start()
        assert instance.integrations.incoming_uploads is not None
        assert not spool.exists()
        assert instance.journal.db.execute(
            'SELECT 1 FROM incoming_uploads WHERE id=?', (identifier,)).fetchone() is None
        assert not list(root.iterdir())
        assert instance.config == original and instance.integrations.local_server is None
        assert instance.integrations.upload_cleanup_errors == 0
        send.assert_not_called()
        with pytest.raises(DevError) as failure:
            instance.integrations.upload_service()
        assert failure.value.code == 'FILE_IMPORT_DISABLED'
    finally:
        await instance.integrations.close()
    assert instance.integrations.upload_cleanup_task.done()


@pytest.mark.asyncio
async def test_startup_without_prior_uploads_does_not_create_spool_or_enable_ingress(agent):
    instance, _, _ = agent
    try:
        await instance.integrations.start()
        assert instance.integrations.incoming_uploads is None
        assert not (instance.journal.directory / 'incoming-upload').exists()
        assert instance.journal.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='incoming_uploads'").fetchone() is None
    finally:
        await instance.integrations.close()


@pytest.mark.asyncio
async def test_startup_cleanup_error_is_counted_without_enabling_ingress(agent, monkeypatch):
    instance, _, _ = agent
    IncomingUploads(instance.engine)
    monkeypatch.setattr(IncomingUploads, 'cleanup',
                        lambda self: (_ for _ in ()).throw(OSError('synthetic failure')))
    try:
        await instance.integrations.start()
        assert instance.integrations.upload_cleanup_errors == 1
        assert instance.integrations.upload_cleanup_task is not None
        with pytest.raises(DevError) as failure:
            instance.integrations.upload_service()
        assert failure.value.code == 'FILE_IMPORT_DISABLED'
    finally:
        await instance.integrations.close()


@pytest.mark.parametrize('field', ['data', 'data_base64'])
@pytest.mark.parametrize('value', [PRIVATE_BYTES, base64.b64encode(PRIVATE_BYTES).decode()])
def test_chunk_audit_summary_is_metadata_only_even_when_nested(field, value):
    summary = safe_summary({'operation': {'upload_id': UPLOAD, 'offset': 0,
                                         'chunk_sha256': sha(PRIVATE_BYTES), field: value}})
    assert summary['operation'][field] == '<private binary chunk>'
    assert summary['operation']['upload_id'] == UPLOAD
    serialized = json.dumps(summary)
    assert PRIVATE_BYTES.decode() not in serialized
    assert base64.b64encode(PRIVATE_BYTES).decode() not in serialized


@pytest.mark.parametrize('name', sorted(INCOMING_UPLOAD_TOOLS))
def test_internal_byte_tools_cannot_be_model_dispatched_or_catalogued(name, monkeypatch):
    router = ToolRouter(None, None)
    monkeypatch.setattr(router, 'definitions',
                        lambda *args: pytest.fail('Internal tool must fail before gateway resolution'))
    with pytest.raises(DevError) as failure:
        router.resolve(object(), name)
    assert failure.value.code == 'UNKNOWN_TOOL'
    for profile in ('core', 'full', 'coding'):
        assert name not in {definition['name'] for definition in tool_definitions(profile)}
    assert name in TOOLS


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEPIER_FILE_IMPORT_STREAMING', 'true')
    store = Store(tmp_path / 'hub-state')
    seed_owner(store, 'owner', 'owner')
    seed_grant(store, 'wiring-grant', 'owner', scopes=('read', 'write'), projects=('project-a',))
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('node-a','synthetic',?,?)",
                  (store.encrypt(token()), time.time()))
    store.execute("""INSERT INTO projects
        (id,alias,alias_key,device_id,root,description,mode,allow_tasks,created)
        VALUES ('project-a','Project','project','node-a','/synthetic/project','','write',0,?)""",
        (time.time(),))
    instance = Runtime(store)
    instance.wait_seconds = 0
    instance.incoming_files = IncomingFileService(instance)
    principal = Principal('mcp:wiring-grant:test', 'owner', {'read', 'write'},
                          ['project-a'], grant_id='wiring-grant')
    yield instance, principal
    store.close()


async def reserved_operation(runtime, name):
    instance, principal = runtime
    service = instance.incoming_files
    raw = {'project': 'project-a', 'workspace_id': '', 'path': DESTINATION,
           'size': len(PRIVATE_BYTES), 'sha256': sha(PRIVATE_BYTES),
           'idempotency_key': uuid.uuid4().hex}
    row, refreshed = service.reserve_for(principal, raw)
    begin = await instance.invoke('incoming_upload_begin', raw, refreshed)
    service.reconcile_for(refreshed, row, begin, begin=True)
    if name == 'incoming_upload_begin':
        return begin['operation_id']
    args = {'project': 'project-a', 'workspace_id': '', 'upload_id': begin['operation_id'],
            'idempotency_key': uuid.uuid4().hex}
    if name == 'incoming_upload_chunk':
        args.update(offset=0, data=base64.b64encode(PRIVATE_BYTES).decode(),
                    chunk_sha256=sha(PRIVATE_BYTES))
    result = await instance.invoke(name, args, refreshed)
    return result['operation_id']


@pytest.mark.asyncio
@pytest.mark.parametrize('name', sorted(INCOMING_UPLOAD_TOOLS))
@pytest.mark.parametrize('status_only', [False, True])
async def test_receipt_reads_require_the_original_upload_manifest(runtime, name, status_only):
    instance, principal = runtime
    operation = await reserved_operation(runtime, name)
    assert instance.operation_row(operation, principal, status_only=status_only)['id'] == operation
    instance.store.execute('DELETE FROM incoming_file_imports')
    with pytest.raises(DevError) as failure:
        instance.operation_row(operation, principal, status_only=status_only)
    assert failure.value.code == 'FILE_IMPORT_NOT_FOUND'


@pytest.mark.asyncio
@pytest.mark.parametrize('change,code', [
    ("UPDATE projects SET root='/synthetic/rebound' WHERE id='project-a'", 'FILE_IMPORT_MAPPING_CHANGED'),
    ('UPDATE incoming_file_imports SET expires=0', 'FILE_IMPORT_EXPIRED'),
])
async def test_saved_receipt_rechecks_mapping_and_expiry(runtime, change, code):
    instance, principal = runtime
    operation = await reserved_operation(runtime, 'incoming_upload_status')
    assert instance.operation_row(operation, principal)['id'] == operation
    instance.store.execute(change)
    with pytest.raises(DevError) as failure:
        instance.operation_row(operation, principal)
    assert failure.value.code == code
