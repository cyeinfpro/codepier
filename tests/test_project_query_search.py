"""Read-only synchronous search is a facade over the original bounded operation."""
from types import SimpleNamespace
import pytest
from jsonschema import Draft202012Validator
from hub.core_tools import invoke, help_result, result
from hub.tool_discovery import call_visible
from shared.contracts import TOOLS, tool_definitions
from shared.query_contracts import ProjectQuery
from shared.util import DevError


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    {'operation_id': 'original', 'matches': [{'path': 'a.py', 'line': 3, 'text': 'needle'}], 'truncated': False},
    {'operation_id': 'original', 'pending': True, 'state': 'running', 'next': 'task_query'},
])
async def test_search_uses_original_read_operation_once(payload):
    calls = []
    principal = object()
    async def dispatch(name, args, who):
        assert who is principal
        calls.append((name, args))
        TOOLS[name].model.model_validate(args)
        return payload
    args = {'project': 'P', 'operation': 'search', 'query': 'needle', 'limit': 10,
            'options': {'file_glob': '*.py', 'case_sensitive': True}}
    ProjectQuery.model_validate(args)
    value = await invoke(SimpleNamespace(invoke=dispatch), 'workspace', args, principal)
    assert value is payload
    assert len(calls) == 1 and calls[0][0] == 'fs_search'
    assert calls[0][1]['file_glob'] == '*.py'
    public = result('project_query', args, value)['structuredContent']
    schema = next(t['outputSchema'] for t in tool_definitions() if t['name'] == 'project_query')
    Draft202012Validator(schema).validate(public)
    assert public['operation_id'] == 'original'


def test_search_discovery_requires_read_and_exposes_exact_options():
    assert call_visible('project_query', {'operation': 'search'}, {'read'}) is True
    assert call_visible('project_query', {'operation': 'search'}, {'execute'}) is False
    schema = help_result('workspace', 'search', scopes={'read'})['inputSchema']
    Draft202012Validator(schema).validate({'operation': 'search', 'project': 'P', 'query': 'needle',
                                         'options': {'file_glob': '*.py'}})
    with pytest.raises(DevError):
        help_result('workspace', 'search', scopes={'execute'})


@pytest.mark.asyncio
async def test_search_does_not_bypass_denial_or_replay():
    calls = []
    async def deny(name, args, principal):
        calls.append(name)
        raise DevError('INSUFFICIENT_SCOPE', 'denied', 403)
    with pytest.raises(DevError, match='denied'):
        await invoke(SimpleNamespace(invoke=deny), 'workspace',
                     {'operation': 'search', 'project': 'P', 'query': 'needle'}, object())
    assert calls == ['fs_search']
