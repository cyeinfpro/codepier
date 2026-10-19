"""Authentication values are removed before process output is journaled."""
import json
import sys
import uuid

import pytest

from agent.runner import Agent
from shared.secret_output import SecretOutputs
from shared.tool_protocol import wire_version
from shared.util import atomic_json


def test_multiple_credentials_are_redacted_at_every_stream_split():
    values = ['synthetic-github-token-one', 'synthetic-enterprise-token-two']
    source = 'before ' + values[0] + ' between ' + values[1] + ' after'
    expected = 'before <redacted> between <redacted> after'
    for split in range(len(source) + 1):
        output_filter = SecretOutputs(values)
        first = output_filter.feed(source[:split])
        assert not any(value in first for value in values)
        assert first + output_filter.feed(source[split:], final=True) == expected


def test_multiple_credentials_preserve_non_secrets_and_flush_final_prefix():
    output_filter = SecretOutputs(['synthetic-secret', '', None, 'synthetic-secret'])
    assert output_filter.feed('normal output synthetic-se') == 'normal output '
    assert output_filter.feed('', final=True) == 'synthetic-se'
    assert output_filter.redact('normal synthetic-secret text') == 'normal <redacted> text'


@pytest.mark.asyncio
async def test_clean_task_redacts_inherited_tokens_in_stream_and_original_receipt(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()
    names = ['GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN']
    values = ['synthetic-inherited-' + str(number) for number in range(len(names))]
    for name, value in zip(names, values):
        monkeypatch.setenv(name, value)
    source = (
        "import os,pathlib\n"
        "p=pathlib.Path('runs'); p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
        + '\n'.join('print(os.environ[' + repr(name) + '], flush=True)' for name in names)
    )
    config_path = tmp_path / 'config.json'
    atomic_json(config_path, {
        'hub_url': 'http://127.0.0.1:9', 'device_id': 'credential-test-node',
        'secret': 'synthetic-credential-test-only-' * 3, 'state_dir': str(tmp_path / 'state'),
        'allowed_roots': [{'path': str(root), 'writable': True, 'allow_tasks': True}],
        'tasks': {'inspect-credentials': {'projects': ['fixture'], 'command': [sys.executable, '-c', source]}},
        'project_runtimes': {'fixture': {'mode': 'project_clean_v1', 'credentials': {'github_cli': True}}},
    })
    agent = Agent(config_path)
    try:
        operation = uuid.uuid4().hex
        request = {
            'id': operation, 'tool': 'tasks_run', 'tool_contract_version': wire_version('tasks_run'),
            'project': {'root': str(root), 'alias': 'fixture', 'mode': 'write', 'allow_tasks': True},
            'args': {'project': 'fixture', 'task': 'inspect-credentials', 'idempotency_key': uuid.uuid4().hex},
        }
        await agent.handle(request)
        receipt = agent.journal.status(operation)
        assert receipt['result']['ok'], receipt['result']
        data = receipt['result']['data']
        assert data['exit_code'] == 0
        assert data['output'] == '<redacted>\n' * len(names)
        captured = json.dumps([receipt, agent.pending_output.get(operation)])
        assert all(value not in captured for value in values)
        await agent.handle(request)
        assert (root / 'runs').read_text() == 'x'
        assert agent.journal.status(operation)['result']['data'] == data
    finally:
        agent.journal.db.close()
        agent.instance_lock.close()
