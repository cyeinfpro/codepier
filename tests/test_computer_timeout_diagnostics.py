import asyncio
import json
import sys
import time

import pytest

from agent.computer_appserver import AppServerClient
from agent.computer_approvals import AgentApprovals
from agent.telemetry import AgentTelemetry
from agent.journal import Journal
from shared.computer_diagnostics import native_error, safe_detail
from shared.util import DevError


async def provider(tmp_path, reply_delay=0, callbacks=1, malformed_id=None, startup_delay=0):
    script = tmp_path/'rpc.py'
    script.write_text('''import json,sys,time
time.sleep(float(sys.argv[4]))
print('FIXTURE_READY',flush=True)
r=json.loads(sys.stdin.readline())
for i in range(int(sys.argv[2])):
 callback_id=json.loads(sys.argv[3]) if sys.argv[3]!='normal' else str(i)
 print(json.dumps({'id':callback_id,'method':'mcpServer/elicitation/request','params':{'threadId':'thread','serverName':'codepier_computer','mode':'form','message':'Allow Fixture?','requestedSchema':{'type':'object','properties':{}}}}),flush=True)
 response=json.loads(sys.stdin.readline())
time.sleep(float(sys.argv[1]))
print(json.dumps({'id':r['id'],'result':{'reply':response}}),flush=True)
''')
    client = AppServerClient({}, .25)
    client.thread_id = 'thread'
    client.approval_timeout_seconds = .8
    client.process = await asyncio.create_subprocess_exec(sys.executable, str(script), str(reply_delay), str(callbacks),
        'normal' if malformed_id is None else json.dumps(malformed_id), str(startup_delay),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)
    try:
        assert await asyncio.wait_for(client.process.stdout.readline(), 10) == b"FIXTURE_READY\n"
    except BaseException:
        await client.close()
        raise
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_delay", [0, .35])
async def test_human_wait_does_not_consume_native_timeout(tmp_path, startup_delay):
    client = await provider(tmp_path, startup_delay=startup_delay)
    calls = []
    async def approve(message):
        calls.append(message)
        await asyncio.sleep(.4)
        return {'action':'accept', 'content':{'unexpected':'must not forward'}}
    client.approval_handler = approve
    try:
        result = await client.request('mcpServer/tool/call', {})
        assert result['reply']['result'] == {'action':'accept', 'content':{}}
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_native_timeout_still_applies_after_human_decision(tmp_path):
    client = await provider(tmp_path, reply_delay=.45)
    async def approve(message):
        await asyncio.sleep(.4)
        return {'action':'accept'}
    client.approval_handler = approve
    with pytest.raises(DevError) as error:
        await client.request('mcpServer/tool/call', {})
    assert error.value.code == 'COMPUTER_TIMEOUT'
    assert client.counter == 1 and client.closed and client.process.returncode is not None


@pytest.mark.asyncio
async def test_repeated_callbacks_share_one_approval_budget(tmp_path):
    client = await provider(tmp_path, callbacks=3)
    client.approval_timeout_seconds = .3
    decisions = []
    async def approve(message):
        await asyncio.sleep(.2)
        decisions.append('accept')
        return {'action':'accept'}
    client.approval_handler = approve
    started = time.monotonic()
    try:
        result = await client.request('mcpServer/tool/call', {})
        assert decisions == ['accept']
        assert result['reply']['result'] == {'action':'cancel'}
        assert time.monotonic()-started < .7
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('identifier',[True, [], {}, '', None])
async def test_malformed_callback_does_not_request_human_input(tmp_path, identifier):
    client = await provider(tmp_path, malformed_id=identifier if identifier is not None else {'invalid':None})
    calls = []
    async def approve(message):
        calls.append(message)
        return {'action':'accept'}
    client.approval_handler = approve
    with pytest.raises(DevError) as error:
        await client.request('mcpServer/tool/call', {})
    assert error.value.code == 'COMPUTER_PROTOCOL'
    assert not calls and client.closed


@pytest.mark.asyncio
async def test_approval_codepier_send_and_cleanup_are_bounded():
    async def stalled_send(data):
        await asyncio.Event().wait()
    approvals = AgentApprovals(stalled_send)
    result = await asyncio.wait_for(approvals.request({'session_id':'s'}, 'Allow?', lambda:True, .03), .3)
    assert result == {'action':'cancel'} and not approvals.pending


def test_native_errors_are_actionable_without_exporting_provider_text():
    secret = 'token-secret-123 user-content'
    for message, code in [('Ambiguous application '+secret, 'COMPUTER_APP_AMBIGUOUS'),
                          ('Permission denied '+secret, 'COMPUTER_PERMISSION_REQUIRED'),
                          ('Unknown failure '+secret, 'COMPUTER_NATIVE_ERROR')]:
        result = native_error({'code':-32000, 'message':message, 'data':secret})
        assert result.code == code
        assert secret not in str(result) and secret not in repr(vars(result))
    assert safe_detail({'duration_ms':-1,'outcome':[], 'error_code':secret, 'native_error_code':True,
                        'approval_timeout_seconds':float('nan'), 'stderr':secret}) == {}


def test_desktop_phases_roundtrip_without_sensitive_detail(tmp_path):
    journal = Journal(tmp_path/'journal')
    try:
        telemetry = AgentTelemetry(journal)
        telemetry.record('op','approval_wait', outcome='started',approval_timeout_seconds=60,
                         message='private app text', blocked_by=['other-user'])
        telemetry.record('op','approval_decided', outcome='accept',duration_ms=17000, stderr='token')
        rows=telemetry.snapshot('op')
        assert [r['stage'] for r in rows] == ['approval_wait','approval_decided']
        assert rows[0]['detail'] == {'outcome':'started','approval_timeout_seconds':60}
        assert rows[1]['detail'] == {'outcome':'accept','duration_ms':17000}
        assert 'private' not in json.dumps(rows) and 'token' not in json.dumps(rows)
    finally:
        journal.db.close()
