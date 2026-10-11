"""Real child processes and durable Agent receipts; never a production device.

Set CODEPIER_LONG_BUDGET_TEST=1 to run actual 10/30-minute silent commands.
The normal regression uses the same path with short bounded durations.
"""
import asyncio
import json
import os
import shlex
import sys
import time
import uuid
from pathlib import Path

import pytest

from agent.runner import Agent
from shared.bounded_execution import make_budget
from shared.contracts import TOOLS
from shared.tool_protocol import wire_version

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def agent_fixture(directory: Path):
    root = directory / 'project'
    root.mkdir(parents=True)
    config = directory / 'config.json'
    config.write_text(json.dumps({
        'hub_url': 'http://127.0.0.1:1', 'device_id': 'budget-fixture', 'secret': 'synthetic-budget-only-' + 'x' * 40,
        'state_dir': str(directory / 'state'),
        'allowed_roots': [{'path': str(root), 'writable': True, 'allow_tasks': True}],
        'shell': {'enabled': True, 'projects': ['budget-project'], 'command': ['/bin/sh', '-c'],
                  'inherit_env': False, 'max_timeout_seconds': 3600},
    }))
    agent = Agent(config)
    project = {'id': 'budget-project', 'alias': 'budget-project', 'root': str(root),
               'mode': 'write', 'allow_tasks': True, 'device_id': 'budget-fixture'}
    return agent, project, root


async def run_silent(directory, seconds, *, cancel=False):
    agent, project, root = agent_fixture(directory)
    identifier = uuid.uuid4().hex
    now = time.time()
    # A genuine absolute wall clock lease, never advanced by a test monkeypatch.
    lease_seconds = 30 if seconds >= 600 else .5
    budget = make_budget({'id': 'goal', 'approval_id': 'approval', 'expires_at': now + seconds + 120},
        {'id': 'work', 'project_id': project['id'], 'attempt': 1, 'fencing_token': 1,
         'lease_until': now + lease_seconds}, 'grant', now, seconds + 60, seconds + 60)
    code = ('import pathlib,time; p=pathlib.Path("counter"); p.open("a").write("1"); '
            f'time.sleep({seconds}); pathlib.Path("finished").write_text("done")')
    args = TOOLS['exec'].model.model_validate({'project': project['id'],
        'command': shlex.quote(sys.executable) + ' -c ' + shlex.quote(code),
        'timeout_seconds': budget['timeout_seconds'], 'idempotency_key': uuid.uuid4().hex}).model_dump()
    request = {'id': identifier, 'tool': 'exec', 'tool_contract_version': wire_version('exec'),
               'args': args, 'project': project, 'not_after': now + lease_seconds,
               'bounded_execution': budget}
    # Simulate an unavailable transport: the actual Agent outbox owns recovery,
    # not a mock success, heartbeat, restarted command, or fabricated receipt.
    agent.socket = agent.channel = None
    started = time.monotonic()
    job = asyncio.create_task(agent.handle(request))
    agent.jobs[identifier] = job
    try:
        async with asyncio.timeout(10):
            while not (root / 'counter').exists():
                if job.done():
                    await job
                    raise AssertionError(agent.journal.status(identifier))
                await asyncio.sleep(.02)
        if cancel:
            agent.cancel_call(identifier)
        await asyncio.wait_for(job, seconds + 90)
        elapsed = time.monotonic() - started
        result = agent.journal.status(identifier)['result']
        assert result['ok'], result
        assert result['data']['execution_budget_enforced'] is True
        assert result['data']['execution_budget'] == budget
        assert (root / 'counter').read_text() == '1'
        if cancel:
            assert result['data']['cancelled'] and not (root / 'finished').exists()
        else:
            assert elapsed >= seconds and time.time() > budget['consumer_lease_until']
            assert result['data']['exit_code'] == 0 and not result['data']['timed_out']
            assert (root / 'finished').read_text() == 'done'
            assert any(entry['id'] == identifier for entry in agent.journal.outbox())
        await agent.handle(request)
        assert (root / 'counter').read_text() == '1'  # Same request only replays its receipt.
        print(json.dumps({'operation_id': identifier, 'silent_seconds': seconds, 'elapsed_seconds': elapsed,
                          'consumer_lease_until': budget['consumer_lease_until'], 'hard_until': budget['hard_until'],
                          'exit_code': result['data']['exit_code'], 'cancelled': bool(result['data'].get('cancelled')),
                          'execution_count': 1, 'transport_connected': False}), flush=True)
    finally:
        if not job.done():
            agent.cancel_call(identifier)
            await asyncio.wait_for(job, 15)
        agent.jobs.pop(identifier, None)
        if agent.kill_tasks:
            await asyncio.gather(*agent.kill_tasks)
        agent.journal.db.close()
        agent.instance_lock.close()


def test_real_silent_process_keeps_budget_and_replays_only_receipt(tmp_path):
    asyncio.run(run_silent(tmp_path / 'silent', 2))


def test_real_process_explicit_cancel_does_not_reexecute(tmp_path):
    asyncio.run(run_silent(tmp_path / 'cancel', 20, cancel=True))


def test_ten_and_thirty_minute_silent_commands(tmp_path):
    if os.environ.get('CODEPIER_LONG_BUDGET_TEST') != '1':
        pytest.skip('opt-in wall-clock 10/30-minute process verification')

    async def both():
        await asyncio.gather(run_silent(tmp_path / 'ten-minutes', 600),
                             run_silent(tmp_path / 'thirty-minutes', 1800))
    asyncio.run(both())
