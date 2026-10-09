"""Loopback HTTP -> real Runtime -> encrypted Agent -> durable ingress receipts.

Uses the existing isolated-process stack, with only generated fixture credentials,
temporary node configuration and synthetic binary data. No external file host,
mock Runtime, direct IncomingUploads invocation or production node is involved.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import uuid

import httpx
import pytest

from shared.util import atomic_json
from tests.support import running_stack, wait_for

pytestmark = [pytest.mark.integration, pytest.mark.slow]

CHUNK = 256 * 1024
PRIVATE_MARKER = b'isolated-transport-payload-marker'
OPAQUE = b'\x00\xff' + PRIVATE_MARKER + b'\x80' + bytes(range(256)) * 2053


def sha(data):
    return hashlib.sha256(data).hexdigest()


def rows(path, sql, arguments=()):
    # Read the live WAL through a separate read-only connection; do not construct
    # Journal/Store here, since their initialization can change durable state.
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, arguments)]
    finally:
        connection.close()


@pytest.fixture(scope='module')
def ingress_stack(tmp_path_factory):
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv('CODEPIER_FILE_IMPORT_STREAMING', 'true')
        environment.delenv('CODEPIER_NATIVE_FILE_RELAY', raising=False)
        with running_stack(tmp_path_factory.mktemp('binary-ingress-transport')) as stack:
            stack.stop_agent()
            stack.config['integrations'] = {'file_import_streaming': True}
            atomic_json(stack.config_path, stack.config)
            stack.start_agent()
            grant = stack.must(stack.client.post('/api/grants', json={
                'label': 'Binary ingress transport fixture',
                'scopes': ['read', 'write'],
                'projects': [stack.project['id']], 'days': 1,
            }))
            # No execute permission is available to these ingress requests.
            stack.ingress_token = grant['token']
            stack.ingress_grant = grant['grant_id']
            yield stack


def resolved_http(stack, response):
    assert response.is_success, f'{response.status_code}: {response.text}'
    assert 'no-store' in response.headers['cache-control'].split(', ')
    assert 'public' not in response.headers['cache-control']
    data = response.json()
    assert not data.get('error'), data
    identifier = data['operation_id']
    if data.get('pending'):
        def terminal():
            reply = stack.mcp('process', {
                'operation': 'wait', 'operation_ids': [identifier],
                'wait_seconds': 5, 'include_output': False,
            }, stack.ingress_token)
            assert not reply.get('isError'), reply
            operation = reply['structuredContent']['operations'][0]
            return operation if not operation.get('pending') else None
        operation = wait_for(terminal, timeout=45)
        assert operation['state'] == 'succeeded', operation
        assert operation['result']['ok'] is True
        data = {**data, **operation['result']['data'], 'pending': False}
    return data


def verify_journal_delivery(stack, operations):
    agent_db = stack.directory / 'agent-state' / 'agent.sqlite3'
    hub_db = stack.hubdir / 'hub.sqlite3'
    identifiers = tuple(operations)
    placeholders = ','.join('?' for _ in identifiers)

    def all_acknowledged():
        saved = rows(agent_db,
                     f'SELECT id,tool,status,acked,result,output FROM calls WHERE id IN ({placeholders})',
                     identifiers)
        return saved if len(saved) == len(identifiers) and all(
            record['status'] == 'done' and record['acked'] == 1 for record in saved) else None

    agent_calls = wait_for(all_acknowledged, timeout=20)
    hub_calls = rows(hub_db,
                    f'SELECT id,tool,state,grant_id,project_id,result,args_summary,output FROM operations WHERE id IN ({placeholders})',
                    identifiers)
    assert len(hub_calls) == len(identifiers)
    for record in agent_calls:
        assert record['tool'] == operations[record['id']]
        assert json.loads(record['result'])['ok'] is True
    for record in hub_calls:
        assert record['tool'] == operations[record['id']]
        assert record['state'] == 'succeeded'
        assert record['grant_id'] == stack.ingress_grant
        assert record['project_id'] == stack.project['id']
        assert json.loads(record['result'])['ok'] is True
        if record['tool'] == 'incoming_upload_chunk':
            # Runtime summaries contain metadata only, never the internal
            # encrypted transport's base64 body.
            assert json.loads(record['args_summary'])['data'] == '<private binary chunk>'
    saved_text = json.dumps(agent_calls + hub_calls)
    assert PRIVATE_MARKER.decode() not in saved_text
    assert base64.b64encode(OPAQUE[:CHUNK]).decode() not in saved_text


@pytest.mark.parametrize('payload,path,restart', [
    (b'', 'transport/空文件.bin', False),
    (OPAQUE, 'transport/附件 🧪.opaque-extension', True),
], ids=['empty-real-transport', 'unicode-multi-chunk-restart'])
def test_real_http_ingress_reaches_agent_and_durable_outbox(ingress_stack, payload, path, restart):
    stack = ingress_stack
    operations = {}
    with httpx.Client(base_url=stack.url, timeout=45, trust_env=False,
                      headers={'Authorization': 'Bearer ' + stack.ingress_token}) as client:
        args = {'project': stack.project['id'], 'path': path, 'size': len(payload),
                'sha256': sha(payload), 'workspace_id': '',
                'idempotency_key': 'transport-' + uuid.uuid4().hex}
        started = resolved_http(stack, client.post('/api/file-imports', json=args))
        upload_id = started['upload_id']
        assert upload_id == started['operation_id']
        assert started['bytes'] == len(payload) and started['received'] == 0
        assert started['created'] is False and started['state'] == 'receiving'
        operations[started['operation_id']] = 'incoming_upload_begin'
        duplicate = resolved_http(stack, client.post('/api/file-imports', json=args))
        assert duplicate['upload_id'] == upload_id
        assert duplicate['operation_id'] == started['operation_id']

        for offset in range(0, len(payload), CHUNK):
            block = payload[offset:offset + CHUNK]
            endpoint = f'/api/file-imports/{upload_id}/chunks?offset={offset}'
            headers = {'Content-Type': 'application/octet-stream', 'X-Chunk-Sha256': sha(block)}
            receipt = resolved_http(stack, client.put(endpoint, headers=headers, content=block))
            operations[receipt['operation_id']] = 'incoming_upload_chunk'
            assert receipt['received'] == offset + len(block)
            assert receipt['created'] is False
            if offset == 0:
                repeated = resolved_http(stack, client.put(endpoint, headers=headers, content=block))
                assert repeated['operation_id'] == receipt['operation_id']
                assert repeated['received'] == receipt['received']
                if restart:
                    # Durable Agent SQLite, encrypted reconnect and Runtime
                    # dispatch all participate; no in-memory fake is retained.
                    stack.stop_agent()
                    stack.start_agent()
                    resumed = resolved_http(stack, client.get(f'/api/file-imports/{upload_id}'))
                    operations[resumed['operation_id']] = 'incoming_upload_status'
                    assert resumed['upload_id'] == upload_id
                    assert resumed['received'] == CHUNK

        finished = resolved_http(stack, client.post(f'/api/file-imports/{upload_id}/finish'))
        operations[finished['operation_id']] = 'incoming_upload_finish'
        assert finished['state'] == 'complete'
        assert finished['created'] is True and finished['ready'] is True
        assert finished['bytes'] == finished['received'] == len(payload)
        assert finished['sha256'] == sha(payload)
        repeated = resolved_http(stack, client.post(f'/api/file-imports/{upload_id}/finish'))
        assert repeated['operation_id'] == finished['operation_id']
        assert repeated['sha256'] == finished['sha256']
        current = resolved_http(stack, client.get(f'/api/file-imports/{upload_id}'))
        operations[current['operation_id']] = 'incoming_upload_status'
        assert current['state'] == 'complete' and current['sha256'] == sha(payload)

    target = stack.projectalpha / path
    assert target.read_bytes() == payload
    assert sha(target.read_bytes()) == finished['sha256']
    if os.name != 'nt':
        assert target.stat().st_mode & 0o111 == 0
    assert not list(stack.projectalpha.rglob('.rd-import-*'))
    assert not (stack.directory / 'agent-state' / 'incoming-upload' / (upload_id + '.part')).exists()
    verify_journal_delivery(stack, operations)
    receipt = rows(stack.hubdir / 'hub.sqlite3',
                   'SELECT state,sha256,bytes,grant_id FROM incoming_file_imports WHERE upload_id=?',
                   (upload_id,))
    assert receipt == [{'state': 'finished', 'sha256': sha(payload),
                        'bytes': len(payload), 'grant_id': stack.ingress_grant}]


def test_explicit_off_hub_rejects_before_runtime_or_agent_admission(tmp_path):
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv('CODEPIER_FILE_IMPORT_STREAMING', 'false')
        environment.delenv('CODEPIER_NATIVE_FILE_RELAY', raising=False)
        with running_stack(tmp_path / 'disabled-ingress-transport') as stack:
            response = stack.client.post('/api/file-imports',
                headers={'Authorization': 'Bearer ' + stack.pat},
                json={'project': stack.project['id'], 'path': 'must-not-exist.bin',
                      'size': 0, 'sha256': sha(b''), 'idempotency_key': uuid.uuid4().hex})
            assert response.status_code == 404
            assert response.json()['error']['code'] == 'FILE_IMPORT_DISABLED'
            assert not (stack.projectalpha / 'must-not-exist.bin').exists()
            assert rows(stack.hubdir / 'hub.sqlite3',
                        "SELECT id FROM operations WHERE tool LIKE 'incoming_upload_%'") == []
            agent_db = stack.directory / 'agent-state' / 'agent.sqlite3'
            assert rows(agent_db, "SELECT id FROM calls WHERE tool LIKE 'incoming_upload_%'") == []
            assert not (stack.directory / 'agent-state' / 'incoming-upload').exists()
