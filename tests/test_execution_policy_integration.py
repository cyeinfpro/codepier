"""Loopback-only integration; the 'codex' here is a shell stub, never a model CLI."""
from __future__ import annotations
import os
import uuid

import httpx

from shared.util import atomic_json


def test_mcp_denial_and_panel_exemption_through_real_authenticated_transport(stack):
    stack.stop_agent()
    stack.hub.terminate()
    stack.hub.wait(timeout=12)
    stack.env['MCP_BLOCK_LOCAL_CODEX'] = 'true'
    stack.start_hub()
    stack.login()
    stub = stack.imago / 'codex'
    stub.write_text('#!/bin/sh\nprintf fake-only\nprintf once >> stub-count\n')
    stub.chmod(0o700)
    stack.config['shell'] = {'enabled':True, 'projects':['Imago'], 'command':['/bin/sh','-c']}
    stack.config['mcp_policy'] = {'block_local_codex':True}
    stack.config['tasks']['protected-stub'] = {'command':[str(stub)], 'projects':['Imago']}
    atomic_json(stack.config_path, stack.config)
    stack.start_agent()

    # Hub rejects the invocation before dispatch; no authentic model binary is used.
    args = {'project':'Imago', 'command':str(stub), 'idempotency_key':uuid.uuid4().hex}
    rejected = stack.mcp('exec', {**(args), 'yield_seconds': 0})
    assert rejected['isError'] and rejected['structuredContent']['error']['code'] == 'CODEX_REMOTE_DISABLED'
    assert not (stack.imago/'stub-count').exists()

    # The task name does not disclose its executable; the Agent inspects local argv.
    receipt = stack.mcp('exec', {'project': 'Imago', 'task': 'protected-stub', 'idempotency_key': uuid.uuid4().hex, 'yield_seconds': 0})['structuredContent']
    rejected_task = stack.poll(receipt['operation_id'])
    assert rejected_task['result']['error']['code'] == 'CODEX_REMOTE_DISABLED'
    assert not (stack.imago/'stub-count').exists()

    # Static lookup and text mentioning Codex remain legitimate MCP commands.
    benign = stack.mcp('exec', {'project': 'Imago', 'command': "printf '%s' 'codex'", 'idempotency_key': uuid.uuid4().hex, 'yield_seconds': 0})['structuredContent']
    completed = stack.poll(benign['operation_id'])
    assert completed['state'] == 'succeeded' and completed['output'] == 'codex'
    info = stack.mcp('workspace', {'project': 'Imago', 'operation': 'status'})['structuredContent']['execution_policy']
    assert info['effective_block_codex'] and info['local_block_codex'] and not info['os_sandbox']
    read = stack.mcp('read', {'project':'Imago', 'path':'README.md'})
    assert not read.get('isError', False)
    normal_task = stack.mcp('exec', {'project': 'Imago', 'task': 'smoke', 'idempotency_key': uuid.uuid4().hex, 'yield_seconds': 0})['structuredContent']
    assert stack.poll(normal_task['operation_id'])['state'] == 'succeeded'

    # Cookie+CSRF-authenticated owner invocation is still allowed, using only our stub.
    admin_receipt = stack.call('shell_exec', {**args, 'idempotency_key':uuid.uuid4().hex})
    admin_result = stack.poll(admin_receipt['operation_id'])
    assert admin_result['state'] == 'succeeded' and admin_result['output'] == 'fake-only'
    assert (stack.imago/'stub-count').read_text() == 'once'
    admin_info = stack.call('execution_info', {'project':'Imago'})['execution_policy']
    assert admin_info['local_block_codex'] and not admin_info['effective_block_codex']

    # Bearer credentials cannot be substituted for the native panel's login cookie.
    with httpx.Client(base_url=stack.url, trust_env=False) as client:
        response = client.get('/api/native/sessions', headers={'Authorization':'Bearer '+stack.pat})
        assert response.status_code == 401
        response = client.post('/api/native/rename', headers={'Authorization':'Bearer '+stack.pat}, json={'project':'Imago', 'args':{}})
        assert response.status_code == 401


def test_agent_local_floor_still_blocks_with_hub_switch_disabled(stack):
    stack.stop_agent()
    stack.hub.terminate()
    stack.hub.wait(timeout=12)
    stack.env['MCP_BLOCK_LOCAL_CODEX'] = 'false'
    stack.start_hub()
    stack.login()
    stub = stack.imago/'codex'
    stub.write_text('#!/bin/sh\nprintf unexpected > never-created\n')
    stub.chmod(0o700)
    stack.config['shell'] = {'enabled':True, 'projects':['Imago'], 'command':['/bin/sh','-c']}
    stack.config['mcp_policy'] = {'block_local_codex':True}
    atomic_json(stack.config_path, stack.config)
    stack.start_agent()
    receipt = stack.mcp('exec', {'project': 'Imago', 'command': str(stub), 'idempotency_key': uuid.uuid4().hex, 'yield_seconds': 0})['structuredContent']
    result = stack.poll(receipt['operation_id'])
    assert result['result']['error']['code'] == 'CODEX_REMOTE_DISABLED'
    assert not (stack.imago/'never-created').exists()
