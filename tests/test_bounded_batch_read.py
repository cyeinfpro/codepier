"""Bounded batch reads use the same file guards, authorization and receipts."""
from __future__ import annotations

import copy
import json
import os
import time
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from starlette.responses import JSONResponse

from agent.core_files import CoreFiles
from agent.filesystem import FileEngine
from agent.resource_queue import Claim, canonical_path, claims_for, overlaps
from hub.core_tools import bound_batch_response, help_result, result as core_result
from hub.runtime import Principal, Runtime
from hub.store import Store
from shared.contracts import OUTPUT_SCHEMAS, TOOLS, tool_definitions
from shared.core_contracts import Read, READ_BATCH_MAX_ITEMS, READ_BATCH_PAYLOAD_BYTES
from shared.crypto import digest, token
from shared.mcp_protocol import complete
from shared.util import DevError
from tests.legacy_iam_fixture import seed_grant, seed_owner


@pytest.fixture
def files(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    engine = FileEngine(
        {'allowed_roots': [{'path': str(root), 'writable': False}]},
        SimpleNamespace(directory=tmp_path / 'journal'),
        tmp_path / 'agent-config.json')
    project = {'root': str(root), 'alias': 'Fixture', 'mode': 'read'}
    return CoreFiles(engine), project, root


def batch(files, items):
    api, project, _ = files
    args = Read(project='Fixture', operation='batch',
                options={'items': items}).model_dump()
    return api.call('read', project, args)


def envelope(value, *, modern=False, poll=False, padding=''):
    value = {**value, 'operation_id': 'original-read-operation'}
    if poll:
        value = {'operations': [{'id': 'original-read-operation', 'operation_id': 'original-read-operation',
                                 'tool': 'read', 'state': 'succeeded',
                                 'result': {'ok': True, 'data': value}}]}
    rendered = core_result('task_query' if poll else 'read',
                           {'operation': 'get' if poll else 'batch'}, value)
    rendered['_meta'] = {'codepier/testPadding': padding}
    if modern:
        rendered = complete(rendered)
    return {'jsonrpc': '2.0', 'id': '请求-identifier', 'result': rendered}


@pytest.mark.parametrize('items', [[], [{'path': 'a'}] * (READ_BATCH_MAX_ITEMS + 1),
                                  [{'path': 'a', 'limit': 2001}],
                                  [{'path': 'a', 'offset': 0}],
                                  [{'path': 'a', 'expected_sha256': 'bad'}]])
def test_invalid_batch_shape(items):
    with pytest.raises(ValidationError):
        Read(project='Fixture', operation='batch', options={'items': items})


@pytest.mark.parametrize('field', ['project', 'workspace_id', 'grant_id', 'space_id', 'operation', 'idempotency_key'])
def test_items_cannot_override_authority_or_operation(field):
    with pytest.raises(ValidationError):
        Read(project='Fixture', operation='batch',
             options={'items': [{'path': 'a', field: 'other'}]})


def test_batch_has_no_top_level_file_parameters_and_revalidates():
    with pytest.raises(ValidationError):
        Read(project='Fixture', operation='batch', path='a', options={'items': [{'path': 'b'}]})
    with pytest.raises(ValidationError):
        Read(project='Fixture', operation='batch',
             options={'items': [{'path': 'a'}], 'workspace_id': 'f' * 32})
    call = Read(project='Fixture', workspace_id='a' * 32, operation='batch',
                options={'items': [{'path': 'a'}]})
    assert Read.model_validate(call.model_dump()) == call


def test_help_and_catalog_keep_one_read_only_tool():
    read = next(item for item in tool_definitions('core') if item['name'] == 'read')
    assert read['annotations']['readOnlyHint'] is True
    assert 'batch' in help_result('read')['tools']['read']['operations']
    help_ = help_result('read', 'batch')
    assert help_['arguments_location'] == 'options'
    assert help_['scope'] == 'read'
    assert help_['inputSchema']['properties']['items']['maxItems'] == 8
    Draft202012Validator(help_['inputSchema']).validate({'items': [{'path': 'a'}]})
    assert not {'fs_read_many', 'read_batch'} & {item['name'] for item in tool_definitions('full')}
    assert TOOLS['read'].scope == 'read'


def test_partial_failures_empty_file_and_independent_continuation(files):
    _, _, root = files
    (root / 'a').write_text('第一行\nsecond\nthird\n', encoding='utf-8')
    (root / 'empty').write_bytes(b'')
    result = batch(files, [{'path': 'a', 'limit': 1}, {'path': 'missing'},
                           {'path': 'empty'}, {'path': 'a', 'offset': 2, 'limit': 1}])
    a, missing, empty, second = result['files']
    assert [item['index'] for item in result['files']] == list(range(4))
    assert a['content'] == '第一行\n' and a['next_offset'] == 2
    assert missing['error']['code'] == 'NOT_FOUND'
    assert missing['error']['retryable'] is False
    assert empty['content'] == '' and empty['total_lines'] == 0 and empty['next_offset'] is None
    assert second['content'] == 'second\n' and second['next_offset'] == 3
    resumed = batch(files, [{'path': 'a', 'offset': a['next_offset'], 'expected_sha256': a['sha256']}])
    assert resumed['files'][0]['content'] == 'second\nthird\n'
    (root / 'a').write_text('changed\n', encoding='utf-8')
    conflict = batch(files, [{'path': 'a', 'expected_sha256': a['sha256']}, {'path': 'empty'}])
    assert conflict['files'][0]['error']['code'] == 'SHA_CONFLICT'
    assert conflict['files'][1]['ok']
    rendered = core_result('read', {'operation': 'batch'}, {**result, 'operation_id': 'receipt'})
    assert rendered['isError'] is True
    Draft202012Validator(OUTPUT_SCHEMAS['read']).validate(rendered['structuredContent'])


@pytest.mark.parametrize('path,code', [
    ('../outside', 'INVALID_PATH'), ('/etc/passwd', 'INVALID_PATH'),
    ('C:\\outside', 'INVALID_PATH'), ('.env', 'PROTECTED_PATH'),
    ('.git/config', 'PROTECTED_PATH'), ('.codex/sessions/a', 'PROTECTED_PATH')])
def test_unsafe_paths_fail_only_their_own_item(files, path, code):
    _, _, root = files
    (root / 'safe').write_text('safe')
    result = batch(files, [{'path': path}, {'path': 'safe'}])
    assert result['files'][0]['error']['code'] == code
    assert result['files'][1]['content'] == 'safe'


def test_links_binary_images_invalid_encoding_and_long_lines(files):
    _, _, root = files
    (root / 'source').write_text('safe')
    (root / 'link').symlink_to(root / 'source')
    os.link(root / 'source', root / 'hardlink')
    (root / 'binary').write_bytes(b'a\x00b')
    (root / 'image').write_bytes(b'\x89PNG\r\n\x1a\n')
    (root / 'encoding').write_bytes(b'\xff')
    (root / 'long').write_text('x' * (50 * 1024 + 1))
    (root / 'last').write_text('last')
    result = batch(files, [{'path': path} for path in
                          ('link', 'hardlink', 'binary', 'image', 'encoding', 'long', 'last')])
    assert [item.get('error', {}).get('code') for item in result['files']] == [
        'SYMLINK_BLOCKED', 'HARDLINK_BLOCKED', 'BINARY_FILE', 'BINARY_FILE',
        'ENCODING', 'LINE_TOO_LARGE', None]
    assert result['files'][-1]['content'] == 'last'
    assert '_computer_content' not in json.dumps(result)


def test_queued_claims_cover_all_files_and_recheck_paths(files):
    api, project, root = files
    (root / 'a').write_text('safe')
    (root / 'b').write_text('other')
    args = Read(project='Fixture', operation='batch',
                options={'items': [{'path': 'a'}, {'path': 'b'}]}).model_dump()
    claims = claims_for(api.engine, 'read', project, args, root)
    assert len(claims) == 2
    assert overlaps(claims, [Claim('agent', 'path', canonical_path(root / 'b'), True)])
    assert not overlaps(claims, [Claim('agent', 'path', canonical_path(root / 'b'), False)])
    (root / 'b').unlink()
    (root / 'b').symlink_to(root / 'a')
    result = api.call('read', project, args)
    assert result['files'][1]['error']['code'] == 'SYMLINK_BLOCKED'
    claims = claims_for(api.engine, 'read', project, args, root)
    assert claims == [Claim('agent', 'path', canonical_path(root), False)]


@pytest.mark.parametrize('modern,poll', [(False, False), (True, False), (False, True), (True, True)])
def test_complete_payload_budget_preserves_utf8_and_continuations(files, modern, poll):
    _, _, root = files
    line = '中文"\\\\\t\x01😀' * 20 + '\n'
    original = (line * 300).encode('utf-8')
    for i in range(8):
        (root / str(i)).write_bytes(original)
    value = batch(files, [{'path': str(i)} for i in range(8)])
    before = copy.deepcopy(value)
    payload = bound_batch_response(envelope(value, modern=modern, poll=poll, padding='m' * 16000))
    assert len(JSONResponse(payload).body) <= READ_BATCH_PAYLOAD_BYTES
    rendered = payload['result']
    assert json.loads(rendered['content'][0]['text']) == rendered['structuredContent']
    public = rendered['structuredContent']
    data = public['operations'][0]['result']['data'] if poll else public
    assert any(item.get('budget_limited') for item in data['files'])
    assert data['operation_id'] == 'original-read-operation'
    assert len(data['files']) == 8
    for item in data['files']:
        assert item['sha256'] == digest(original)
        returned = item['content'].splitlines(keepends=True)
        assert all(part == line for part in returned)
        assert item['end_line'] == len(returned)
        assert item['next_offset'] == len(returned) + 1
    assert value == before  # Durable result must not be rewritten.


def test_budget_can_defer_a_whole_line_without_losing_offset(files):
    _, _, root = files
    (root / 'a').write_text('\x01' * 30000 + '\n')
    value = batch(files, [{'path': 'a'}])
    payload = bound_batch_response(envelope(value))
    item = payload['result']['structuredContent']['files'][0]
    assert item['content'] == ''
    assert item['next_offset'] == item['offset'] == 1
    assert item['end_line'] == 0 and item['budget_limited']
    assert len(JSONResponse(payload).body) <= READ_BATCH_PAYLOAD_BYTES


def test_oversized_unshrinkable_metadata_fails_closed(files):
    _, _, root = files
    (root / 'a').write_text('text')
    with pytest.raises(DevError) as exc:
        bound_batch_response(envelope(batch(files, [{'path': 'a'}]), padding='m' * READ_BATCH_PAYLOAD_BYTES))
    assert exc.value.code == 'READ_BATCH_RESPONSE_TOO_LARGE'


def test_non_batch_result_is_unchanged():
    payload = {'jsonrpc': '2.0', 'id': 1,
               'result': {'structuredContent': {'content': 'ordinary file'}}}
    assert bound_batch_response(payload) is payload



def test_os_denial_is_not_retried_and_keeps_other_items(files, monkeypatch):
    api, _, root = files
    (root / 'safe').write_text('readable')
    attempts = []
    original = api.engine.read_bytes

    def read(path, *args, **kwargs):
        attempts.append(path.name)
        if path.name == 'denied':
            raise PermissionError('private absolute path must not leak')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(api.engine, 'read_bytes', read)
    result = batch(files, [{'path': 'denied'}, {'path': 'safe'}])
    assert attempts == ['denied', 'safe']
    assert result['files'][0]['error'] == {
        'code': 'FILE_READ_FAILED', 'message': '无法读取此文件', 'retryable': False}
    assert result['files'][1]['content'] == 'readable'
    assert 'private absolute' not in json.dumps(result)


def test_file_size_and_invalid_offset_are_independent_errors(files):
    _, _, root = files
    with (root / 'large').open('wb') as stream:
        stream.truncate(16 * 1024 * 1024 + 1)
    (root / 'small').write_text('one\n')
    result = batch(files, [{'path': 'large'}, {'path': 'small', 'offset': 2}, {'path': 'small'}])
    assert result['files'][0]['error']['code'] == 'FILE_TOO_LARGE'
    assert result['files'][1]['error']['code'] == 'INVALID_OFFSET'
    assert result['files'][2]['content'] == 'one\n'


@pytest.mark.asyncio
async def test_original_read_authorization_and_durable_operation(tmp_path):
    store = Store(tmp_path / 'hub')
    try:
        seed_owner(store, 'owner', 'admin')
        seed_grant(store, 'reader', 'owner', scopes=('read',), projects=('proj',))
        store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('dev','test',?,?)",
                      (store.encrypt(token()), time.time()))
        store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,description,mode,allow_tasks,created) "
                      "VALUES ('proj','Fixture','fixture','dev','/tmp/fixture','','read',0,?)", (time.time(),))
        runtime = Runtime(store)
        runtime.wait_seconds = 0
        principal = Principal('mcp:reader:test', 'owner', {'read'}, ['proj'], grant_id='reader')
        args = {'project': 'Fixture', 'operation': 'batch',
                'options': {'items': [{'path': 'a'}, {'path': 'b', 'offset': 2}]},
                'idempotency_key': 'bounded-read-original-operation'}
        receipt = await runtime.invoke('read', args, principal)
        repeated = await runtime.invoke('read', args, principal)
        assert repeated['operation_id'] == receipt['operation_id']
        row = store.one('SELECT * FROM operations WHERE id=?', (receipt['operation_id'],))
        request = json.loads(store.decrypt(row['payload']))
        assert row['tool'] == 'read' and row['grant_id'] == 'reader' and row['space_id'] == 'legacy'
        assert request['args']['operation'] == 'batch'
        assert request['args']['options']['items'][1]['offset'] == 2
        polled = await runtime.invoke('task_query', {'operation': 'get',
                                      'operation_ids': [receipt['operation_id']]}, principal)
        assert polled['operations'][0]['id'] == receipt['operation_id']
        assert len(store.all('SELECT id FROM operations')) == 1
        store.execute("UPDATE grants SET scopes='[]' WHERE id='reader'")
        with pytest.raises(DevError) as denied:
            await runtime.invoke('read', args, principal)
        # The persisted fixed grant is invalid once its read scope is removed.
        assert denied.value.code == 'INVALID_TOKEN'
        assert len(store.all('SELECT id FROM operations')) == 1
    finally:
        store.close()
