"""Factored public schemas, historical validation and refusal fidelity."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from jsonschema import Draft202012Validator, ValidationError
from agent.project_runtime import descriptor, validate_runtimes
from agent.source_versions import Validations
from shared.core_output_schemas import factor
from shared.mcp_presentation import output_schema, present
from shared.runtime_results import decorate_result, failure_context
from tests.test_shell import shell_agent, shell_request  # noqa: F401


def test_factored_tools_map_changes_without_changing_other_string_maps():
    paths = {'type': 'object', 'additionalProperties': {'anyOf': [{'type': 'string'}, {'type': 'null'}]}}
    original = {'type': 'object', 'properties': {'tools': paths, 'paths': paths}, 'required': ['tools', 'paths']}
    reduced = factor(original)
    assert '$defs' in reduced
    public = output_schema('project_query', reduced)
    Draft202012Validator(public).validate({'tools': {'git': True}, 'paths': {'git': '/usr/bin/git'}})
    with pytest.raises(ValidationError):
        Draft202012Validator(public).validate({'tools': {'git': '/usr/bin/git'}, 'paths': {'git': '/usr/bin/git'}})
    with pytest.raises(ValidationError):
        Draft202012Validator(public).validate({'tools': {'git': True}, 'paths': {'git': True}})
    Draft202012Validator(reduced).validate({'tools': {'git': '/usr/bin/git'}, 'paths': {'git': '/usr/bin/git'}})


def test_validation_becomes_stale_after_runtime_change_without_rewriting_history(shell_agent, monkeypatch):
    agent, root = shell_agent
    project = shell_request(root)['project']
    agent.config['project_runtimes'] = validate_runtimes({'fixture': {'mode': 'project_clean_v1'}})
    source = {'complete': True, 'sha256': 'a' * 64}
    record = {'validation_id': 'id', 'state': 'passed', 'after': source,
              'runtime': descriptor(agent.config, project, root), 'decision': {'action': 'accept'}}
    before = deepcopy(record)
    records = SimpleNamespace(load=lambda *args: deepcopy(record))
    monkeypatch.setattr('agent.source_versions.source_version', lambda *args: deepcopy(source))
    service = Validations(agent, records)
    initial = service.get(project, 'id')
    assert initial['state'] == 'passed' and initial['runtime_current'] and initial['accepted_current']
    agent.config['project_runtimes'] = validate_runtimes({'fixture': {'mode': 'project_clean_v1', 'env': {'CHANGED': '1'}}})
    current = service.get(project, 'id')
    assert current['state'] == 'stale' and not current['runtime_current'] and current['source_current']
    assert current['historical_state'] == 'passed' and not current['accepted_current']
    assert record == before


@pytest.mark.parametrize('code,category', [('CONVERSATION_ARCHIVED', 'codepier_authorization'),
    ('RUNTIME_CREATION_UNCERTAIN', 'outcome_unconfirmed'), ('COMMAND_MISSING', 'execution_unavailable')])
def test_project_failures_keep_real_domain_and_never_grant_approval(code, category):
    result = failure_context(error={'code': code}, operation_id='original')
    assert result['category'] == category and result['code'] == code
    assert not result['grants_authority'] and not result['replay_recommended']
    assert result['caller_platform_decision'] == 'not_observed'


def test_direct_poll_and_standard_task_keep_one_runtime_without_unknown_metadata():
    import json
    from hub.mcp_tasks import render_exec_v1
    operation = 'a' * 32
    runtime = {'schema_version': 1, 'mode': 'project_clean_v1', 'runtime_ref': 'b' * 64,
        'revision': 'c' * 64, 'grants_authority': False, 'isolation': 'none', 'device_id': 'PRIVATE-SENTINEL'}
    body = '/Users/SYNTHETIC/source.py:1: failure\n'
    raw = decorate_result({'exit_code': 7, 'output': body, 'command_ok': False},
        {'id': 'project'}, operation, runtime, '/Users/SYNTHETIC')
    raw['runtime']['unknown_host_metadata'] = 'PRIVATE-SENTINEL'
    direct = present('exec', {}, {'operation_id': operation, **raw})
    polled = present('task_query', {'operation': 'get'}, {'operations': [{'id': operation,
        'project_id': 'project', 'tool': 'exec', 'state': 'failed', 'result': {'ok': True, 'data': raw}}]})
    task = render_exec_v1({'operation_id': operation, 'state': 'failed',
                           'result': json.dumps({'ok': True, 'data': raw})})
    for row in (direct, polled['operations'][0]['result']['data'], task['structuredContent']):
        assert row['output'] == body and row['exit_code'] == 7
        assert row['continuation']['operation_id'] == operation
        assert 'PRIVATE-SENTINEL' not in json.dumps(row)
    assert task['isError']


@pytest.mark.asyncio
@pytest.mark.parametrize('container', [False, True])
async def test_validation_preserves_only_actual_runtime_evidence(shell_agent, monkeypatch, container):
    agent, root = shell_agent
    project = shell_request(root)['project']
    source = {'complete': True, 'sha256': 'a' * 64}
    monkeypatch.setattr('agent.source_versions.source_version', lambda *args: dict(source))
    payload = {'exit_code': 0, 'output': 'verified', 'runtime': descriptor(agent.config, project, root)}
    if container:
        payload.update(runtime_outcome={'state': 'confirmed'}, runtime_cleanup={'pending': False, 'state': 'removed'})
    async def execute(*args):
        return deepcopy(payload)
    monkeypatch.setattr(agent, 'execute', execute)
    saved = []
    records = SimpleNamespace(save=lambda *args, **kwargs: saved.append(deepcopy(args[-1])))
    result = await Validations(agent, records).run('validation-fixture', project,
        {'project': 'fixture', 'cwd': '.', 'command': 'true', 'timeout_seconds': 10, 'env': {}, 'label': 'fixture'})
    assert result['state'] == 'passed'
    for value in (result, saved[-1]):
        for name in ('runtime_outcome', 'runtime_cleanup'):
            if container:
                assert value[name] == payload[name]
            else:
                assert name not in value
