"""Read-only synchronous search is a facade over the original bounded operation."""
from types import SimpleNamespace
import pytest
from jsonschema import Draft202012Validator
from hub.core_tools import invoke, help_result, result
from hub.tool_discovery import call_visible
from shared.contracts import TOOLS, tool_definitions
from shared.query_contracts import ProjectQuery
from shared.util import DevError
from tests.test_agentdock_context import workspace  # noqa: F401 (fixture)


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


def test_sync_search_distinguishes_empty_scope_empty_matches_and_page_limit(workspace):
    engine, project, root = workspace
    (root / 'src').mkdir(exist_ok=True)
    (root / 'src' / 'code.py').write_text('needle\nneedle\n')
    def search(query='needle', file_glob='*', **options):
        args = TOOLS['fs_search'].model.model_validate(
            {'project': 'P', 'path': 'src', 'query': query, 'file_glob': file_glob, **options})
        return engine.search(project, args.model_dump())
    empty = search(file_glob='code.py')
    assert empty['scan_status'] == 'no_files' and empty['scanned_files'] == 0
    assert 'file_glob' in empty['hint']
    unmatched = search(query='other', file_glob='src/*.py')
    assert unmatched['scan_status'] == 'no_matches' and unmatched['scanned_files'] >= 1
    first = search(file_glob='*code.py', limit=1)
    assert first['scan_status'] == 'page_limited' and first['next_offset'] == 1
    last = search(file_glob='*code.py', limit=1, offset=1)
    assert last['scan_status'] == 'matches' and not last['truncated']
