"""Precision and recovery regressions for invocation filtering; never starts Codex."""
import asyncio
import sys
import time

import pytest

from shared.execution_policy import InvocationInspector
from shared.util import DevError
from hub.runtime import Principal
from tests.test_audit_runtime import runtime, Socket, until
from tests.test_shell import shell_agent, shell_request


@pytest.mark.parametrize('command', [
    "env RUNNER=codex sh -c '$RUNNER exec task'",
    "RUNNER=codex sh -c '$RUNNER exec task'",
    "export RUNNER=codex; sh -c '$RUNNER exec task'",
    "alias helper=codex; helper exec task",
    "alias first=second; alias second=codex; first exec task",
    "npx --call 'codex exec task'",
    "node -e 'const cp = require(\"node:child_process\"); cp.spawn(\"codex\", [])'",
    "node -e 'const {spawn: launch} = require(\"child_process\"); launch(\"codex\", [])'",
    "node -e 'import {spawn as launch} from \"node:child_process\"; launch(\"codex\", [])'",
    "node -e 'require(\"child_process\").spawn(\"sh\", [\"-c\", \"codex exec task\"])'",
])
def test_wrapper_and_import_aliases_are_detected(command):
    assert InvocationInspector().shell(command)


@pytest.mark.parametrize('command', [
    'npx --yes echo codex', 'npm exec -- echo codex',
    'Start-Process echo -ArgumentList codex',
    "node -e '/x/.exec(\"codex\")'",
    "node -e 'const runner = {spawn: console.log}; runner.spawn(\"codex\")'",
    "alias helper=codex; printf ok",
    "RUNNER=codex printf ok; $RUNNER text",
])
def test_data_positions_and_temporary_environment_do_not_false_positive(command):
    assert not InvocationInspector(env={'RUNNER':'printf'}).shell(command)


def test_environment_unset_and_shell_local_variables():
    inspect = InvocationInspector(env={'RUNNER':'codex'})
    assert not inspect.shell("env -u RUNNER sh -c '$RUNNER'")
    assert not inspect.shell("env -i sh -c '$RUNNER'")
    assert inspect.env['RUNNER'] == 'codex'
    inspect = InvocationInspector()
    assert not inspect.shell("RUNNER=codex; sh -c '$RUNNER'")  # not exported


def test_bounded_analysis_is_not_a_silent_allow():
    inspect = InvocationInspector()
    inspect.remaining = 4
    with pytest.raises(DevError) as caught:
        inspect.shell('echo one; echo two; codex exec task')
    assert caught.value.code == 'EXECUTION_POLICY_LIMIT'


def test_missing_parser_dependency_is_explicit_and_fail_closed(monkeypatch):
    monkeypatch.setitem(sys.modules, 'tree_sitter_bash', None)
    with pytest.raises(DevError) as caught:
        InvocationInspector().shell('echo normal')
    assert caught.value.code == 'EXECUTION_POLICY_UNAVAILABLE'


@pytest.mark.asyncio
async def test_queue_does_not_retry_missing_grammar_forever(runtime, monkeypatch):
    r, _, _ = runtime
    p = Principal('mcp:test:ChatGPT', 'owner', {'read','execute'}, ['*'])
    monkeypatch.delenv('MCP_BLOCK_LOCAL_CODEX', raising=False)
    receipt = await r.invoke('shell_exec', {'project':'Project', 'command':'echo normal', 'idempotency_key':'missing-parser-queue'}, p)
    monkeypatch.setenv('MCP_BLOCK_LOCAL_CODEX', 'true')
    monkeypatch.setitem(sys.modules, 'tree_sitter_bash', None)
    await r.deliver(receipt['operation_id'])
    op = r.operation(receipt['operation_id'], p)
    assert op['state'] == 'failed' and op['attempts'] == 0
    assert op['result']['error']['code'] == 'EXECUTION_POLICY_UNAVAILABLE'


@pytest.mark.asyncio
@pytest.mark.parametrize('accepted', [False, True])
async def test_ambiguous_or_accepted_work_is_only_probed(runtime, monkeypatch, accepted):
    r, _, secret = runtime
    p = Principal('mcp:test:ChatGPT', 'owner', {'read','execute'}, ['*'])
    monkeypatch.delenv('MCP_BLOCK_LOCAL_CODEX', raising=False)
    receipt = await r.invoke('shell_exec', {'project':'Project', 'command':'codex never-run', 'idempotency_key':'ambiguous-policy-probe'}, p)
    r.store.execute('UPDATE operations SET attempts=1,accepted_at=? WHERE id=?', (time.time() if accepted else None, receipt['operation_id']))
    monkeypatch.setenv('MCP_BLOCK_LOCAL_CODEX', 'true')
    socket = Socket(secret)
    reader = asyncio.create_task(r.agent_socket(socket, 'dev'))
    try:
        await until(lambda: r.online('dev'))
        await r.deliver(receipt['operation_id'])
        assert [x['type'] for x in socket.packets] == ['ready', 'probe']
        assert r.operation(receipt['operation_id'], p)['pending']
    finally:
        await socket.close()
        await reader
