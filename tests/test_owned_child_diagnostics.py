"""Owned-child diagnostics observe, never change process execution or retry work."""
import asyncio
import json
import sys

import httpx
import pytest

from agent.journal import Journal
from agent.telemetry import AgentTelemetry
from hub.diagnostics import event_detail
from shared.process_diagnostics import safe_process_detail
from tests.test_audit_agent import local_agent
from tests.operation_diagnostics import failure_note


def test_only_structural_owned_child_fields_survive():
    detail = {'pid': 123, 'returncode': None, 'reader_done': False,
              'argv': 'SECRET', 'env': 'SECRET', 'output': 'SECRET', 'password': 'SECRET'}
    expected = {'pid': 123, 'returncode': None, 'reader_done': False}
    assert safe_process_detail(detail) == expected
    assert event_detail('process_spawned', detail) == expected
    assert safe_process_detail({'pid': True, 'returncode': 'SECRET', 'reader_done': 1}) == {}
    assert safe_process_detail({'pid': -1, 'returncode': 2**40}) == {}
    assert safe_process_detail('SECRET') == {}


def test_trace_authorization_precedes_any_owned_child_evidence_read():
    from types import SimpleNamespace
    from hub.diagnostics import Diagnostics
    from shared.util import DevError
    principal = object()
    calls = []
    def denied(identifier, who, options):
        calls.append((identifier, who))
        raise DevError('FORBIDDEN', 'not visible', 403)
    diagnostics = object.__new__(Diagnostics)
    # Deliberately no store: an unauthorized read must not reach persistence.
    diagnostics.runtime = SimpleNamespace(operation=denied)
    with pytest.raises(DevError, match='not visible'):
        diagnostics.trace({'operation_id': 'unrelated-operation', 'after_event_id': 0, 'limit': 32}, principal)
    assert calls == [('unrelated-operation', principal)]


def test_process_events_never_write_or_wait_on_diagnostic_lock(tmp_path):
    journal = Journal(tmp_path / 'journal')
    try:
        telemetry = AgentTelemetry(journal, process_diagnostics=True)
        telemetry.record('op', 'executing')
        before = journal.db.total_changes
        telemetry.process_event('op', 'process_spawning', argv='SECRET')
        telemetry.process_event('op', 'process_spawned', pid=123)
        assert journal.db.total_changes == before
        with telemetry._process_lock:
            telemetry.process_event('op', 'process_stdout_eof', pid=123)
        trace = telemetry.snapshot('op')
        assert [event['stage'] for event in trace] == [
            'executing', 'process_spawning', 'process_spawned']
        assert [event['seq'] for event in trace] == [1, 2, 3]
        assert 'SECRET' not in json.dumps(trace)
        assert telemetry.snapshot('unrelated-operation') == []
        telemetry.record('op', 'result_ready')
        assert not telemetry._process_pending
        for number in range(300):
            for _ in range(20):
                telemetry.process_event(str(number), 'process_spawning')
        assert len(telemetry._process_pending) == 256
        assert max(map(len, telemetry._process_pending.values())) == 16
    finally:
        journal.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('diagnostics_fail', [False, True])
async def test_silent_owned_child_keeps_output_exit_and_cleanup(local_agent, monkeypatch, diagnostics_fail):
    agent, root = local_agent
    agent.telemetry.process_diagnostics = True
    agent.journal.start('silent', {'tool': 'tasks_run'})
    if diagnostics_fail:
        def broken(*args, **kwargs):
            raise RuntimeError('diagnostic fixture failure')
        monkeypatch.setattr(agent.telemetry, 'process_event', broken)
    result = await asyncio.wait_for(
        agent.run_process('silent', [sys.executable, '-c', 'pass'], root, 5), 8)
    assert result['exit_code'] == 0 and result['output'] == ''
    assert not result['timed_out'] and not result['cancelled'] and not agent.processes
    if not diagnostics_fail:
        stages = [event['stage'] for event in agent.telemetry.snapshot('silent')]
        assert stages[:2] == ['process_spawning', 'process_spawned']
        assert {'process_stdout_eof', 'process_leader_exited',
                'process_cleanup_started', 'process_cleanup_finished'} <= set(stages)


@pytest.mark.asyncio
async def test_delayed_spawn_is_distinct_from_running_child(local_agent, monkeypatch):
    agent, root = local_agent
    agent.telemetry.process_diagnostics = True
    import agent.owned_subprocess as owned
    entered, release = asyncio.Event(), asyncio.Event()
    if sys.platform == 'win32':
        original = asyncio.create_subprocess_exec
        target, name = asyncio, 'create_subprocess_exec'
    else:
        original = owned.create_owned_process
        target, name = owned, 'create_owned_process'
    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(target, name, delayed)
    agent.journal.start('delayed', {'tool': 'tasks_run'})
    work = asyncio.create_task(agent.run_process('delayed', [sys.executable, '-c', 'pass'], root, 5))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        trace = agent.telemetry.snapshot('delayed')
        assert [event['stage'] for event in trace] == ['process_spawning']
        assert not agent.processes
    finally:
        release.set()
        result = await asyncio.wait_for(work, 8)
    assert result['exit_code'] == 0


def test_failure_note_bounds_requests_and_filters_all_content():
    calls = []
    def handler(request):
        calls.append(request)
        assert all(value == 2 for value in request.extensions['timeout'].values())
        if request.method == 'GET':
            return httpx.Response(200, json={'state': 'running', 'output': 'SECRET'})
        body = json.loads(request.content)
        assert body['tool'] == 'operations_trace'
        return httpx.Response(200, json={'events': [
            {'stage': 'process_spawned', 'elapsed_ms': 12, 'detail': {'pid': 42, 'argv': 'SECRET'}}]})
    with httpx.Client(base_url='http://fixture.invalid', transport=httpx.MockTransport(handler)) as client:
        note = failure_note(client, 'synthetic-operation')
    assert len(calls) == 2 and 'SECRET' not in note and '"pid": 42' in note


def test_original_poll_failure_survives_unavailable_diagnostics():
    from tests.test_smoke import result
    original = AssertionError('original timeout')
    calls = []
    def unavailable(request):
        calls.append(request)
        raise httpx.ReadTimeout('SECRET')
    class Stack:
        def mcp(self, name, args):
            return {'structuredContent': {'pending': True, 'operation_id': 'synthetic-operation'}}
        def poll(self, identifier, timeout):
            assert timeout == 30
            raise original
    stack = Stack()
    with httpx.Client(base_url='http://fixture.invalid', transport=httpx.MockTransport(unavailable)) as client:
        stack.client = client
        with pytest.raises(AssertionError) as caught:
            result(stack, 'exec', {'command': 'unchanged'})
    assert caught.value is original
    assert len(calls) == 2 and 'unavailable' in original.__notes__[0]
    assert 'SECRET' not in original.__notes__[0]


def test_diagnostic_response_budget_closes_stream_without_reading_rest():
    from tests.operation_diagnostics import bounded_json
    chunks = []
    closed = []
    class Oversized(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(100):
                chunks.append(index)
                yield b'x' * 4096
        def close(self):
            closed.append(True)
    def handler(request):
        return httpx.Response(200, stream=Oversized())
    with httpx.Client(base_url='http://fixture.invalid', transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match='byte budget'):
            bounded_json(client, 'GET', '/synthetic')
    assert len(chunks) == 17 and closed


def test_first_status_evidence_requires_explicit_output_opt_in(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('CODEPIER_TEST_PROCESS_EVIDENCE', raising=False)
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'state': 'running', 'output': 'SECRET'})
        return httpx.Response(200, json={'events': [
            {'stage': 'process_spawning', 'elapsed_ms': 5, 'detail': {'env': 'SECRET'}}]})
    destination = tmp_path / 'dist/windows/first-git-status-trace.json'
    with httpx.Client(base_url='http://fixture.invalid', transport=httpx.MockTransport(handler)) as client:
        failure_note(client, 'synthetic-operation', save_first_status=True)
        assert not destination.exists()
        monkeypatch.setenv('CODEPIER_TEST_PROCESS_EVIDENCE', '1')
        failure_note(client, 'synthetic-operation', save_first_status=True)
    raw = destination.read_text()
    assert len(raw.encode()) <= 10001 and 'SECRET' not in raw
    assert json.loads(raw)['process_trace'][0]['stage'] == 'process_spawning'


def test_process_diagnostics_are_off_by_default(tmp_path):
    journal = Journal(tmp_path / 'journal')
    try:
        telemetry = AgentTelemetry(journal)
        before = journal.db.total_changes
        telemetry.process_event('op', 'process_spawned', pid=123)
        assert telemetry.snapshot('op') == []
        assert not telemetry._process_pending and journal.db.total_changes == before
    finally:
        journal.db.close()


def test_unexpected_diagnostic_failure_cannot_replace_original(monkeypatch):
    from tests.test_smoke import result
    import tests.operation_diagnostics as diagnostics
    original = AssertionError('original timeout')
    def broken(*args):
        raise RuntimeError('diagnostic failure')
    monkeypatch.setattr(diagnostics, 'failure_note', broken)
    class Stack:
        client = None
        def mcp(self, name, args):
            return {'structuredContent': {'pending': True, 'operation_id': 'synthetic-operation'}}
        def poll(self, identifier, timeout):
            raise original
    with pytest.raises(AssertionError) as caught:
        result(Stack(), 'exec', {'command': 'unchanged'})
    assert caught.value is original
